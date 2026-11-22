// Package client keeps the outgoing mTLS WebSocket connection to MCP Management Node open and
// answers the app's requests. The connection is always started by the agent, so nodes behind
// NAT or a firewall need no port forward or VPN.
package client

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"log"
	"math/rand/v2"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"

	"github.com/rhisijmuiden/mcp-management-node/agent/internal/apt"
	"github.com/rhisijmuiden/mcp-management-node/agent/internal/config"
	"github.com/rhisijmuiden/mcp-management-node/agent/internal/governor"
	"github.com/rhisijmuiden/mcp-management-node/agent/internal/ops"
	"github.com/rhisijmuiden/mcp-management-node/agent/internal/proto"
	"github.com/rhisijmuiden/mcp-management-node/agent/internal/pve"
	"github.com/rhisijmuiden/mcp-management-node/agent/internal/selfupdate"
	"github.com/rhisijmuiden/mcp-management-node/agent/internal/sysinfo"
	"github.com/rhisijmuiden/mcp-management-node/agent/internal/tlsutil"
)

var (
	MetricsInterval = 15 * time.Second
	UpdatesInterval = 6 * time.Hour
	ClusterInterval = 5 * time.Minute
)

type Agent struct {
	Version string
	cfg     *config.Config
	tls     *tls.Config
	http    *http.Client

	mu      sync.Mutex
	out     chan proto.Envelope // current connection, nil when offline
	pending []proto.Envelope    // job results to deliver after reconnect

	updMu       sync.Mutex
	lastUpdates *proto.Updates
	guests      *sysinfo.Guests
	guestsAt    time.Time

	exit func(int)
}

// New loads the enrollment from disk.
func New(version string) (*Agent, error) {
	cfg, err := config.Load()
	if err != nil {
		return nil, err
	}
	cert, err := tls.LoadX509KeyPair(config.CertFile(), config.KeyFile())
	if err != nil {
		return nil, fmt.Errorf("client certificate: %w", err)
	}
	caPEM, err := os.ReadFile(config.CAFile())
	if err != nil {
		return nil, err
	}
	blk, _ := pem.Decode(caPEM)
	if blk == nil {
		return nil, errors.New("ca.crt is not PEM")
	}
	ca, err := x509.ParseCertificate(blk.Bytes)
	if err != nil {
		return nil, err
	}
	if cfg.CAFingerprint != "" && tlsutil.Fingerprint(ca.Raw) != cfg.CAFingerprint {
		return nil, errors.New("ca.crt does not match the pinned fingerprint")
	}
	tc := tlsutil.ClientConfig(cert, ca)
	return &Agent{
		Version: version, cfg: cfg, tls: tc,
		http: &http.Client{Timeout: 10 * time.Minute, Transport: &http.Transport{TLSClientConfig: tc, Proxy: http.ProxyFromEnvironment}},
		exit: os.Exit,
	}, nil
}

// Run connects forever, with exponential backoff (1s .. 60s, jittered).
func (a *Agent) Run(ctx context.Context) error {
	a.applyStoredGovernor()
	go a.updatesLoop(ctx)
	backoff := time.Second
	for {
		start := time.Now()
		err := a.session(ctx)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if time.Since(start) > time.Minute {
			backoff = time.Second
		}
		log.Printf("disconnected from %s: %v (retry in %s)", a.cfg.Server, err, backoff)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff + time.Duration(rand.Int64N(int64(backoff/2+1)))):
		}
		backoff = min(backoff*2, 60*time.Second)
	}
}

func (a *Agent) applyStoredGovernor() {
	st := config.LoadState()
	if st.Governor == "" {
		return
	}
	if _, err := governor.Set(st.Governor); err != nil {
		log.Printf("could not apply stored CPU governor %q: %v", st.Governor, err)
	} else {
		log.Printf("CPU governor set to %s", st.Governor)
	}
}

func (a *Agent) session(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	hdr := http.Header{}
	hdr.Set("User-Agent", "mmn-agent/"+a.Version)
	c, _, err := websocket.Dial(ctx, "wss://"+a.cfg.Server+"/agent/ws", &websocket.DialOptions{
		HTTPClient: &http.Client{Timeout: 30 * time.Second, Transport: &http.Transport{TLSClientConfig: a.tls, Proxy: http.ProxyFromEnvironment}},
		HTTPHeader: hdr,
	})
	if err != nil {
		return err
	}
	defer c.CloseNow()
	c.SetReadLimit(8 << 20)
	log.Printf("connected to %s as node %s", a.cfg.Server, a.cfg.NodeID)

	out := make(chan proto.Envelope, 256)
	a.mu.Lock()
	a.out = out
	pending := a.pending
	a.pending = nil
	a.mu.Unlock()
	defer func() {
		a.mu.Lock()
		a.out = nil
		a.mu.Unlock()
	}()

	errc := make(chan error, 3)
	go func() { // writer
		for {
			select {
			case <-ctx.Done():
				return
			case m := <-out:
				b, _ := json.Marshal(m)
				wctx, wcancel := context.WithTimeout(ctx, 30*time.Second)
				err := c.Write(wctx, websocket.MessageText, b)
				wcancel()
				if err != nil {
					if m.Type == "job" && m.Event == "done" {
						a.queuePending(m)
					}
					errc <- err
					return
				}
			}
		}
	}()
	out <- proto.Envelope{Type: "hello", Data: a.hello(ctx)}
	for _, m := range pending {
		out <- m
	}
	go a.tickers(ctx, out)
	go func() { // keepalive
		t := time.NewTicker(30 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				pctx, pcancel := context.WithTimeout(ctx, 20*time.Second)
				err := c.Ping(pctx)
				pcancel()
				if err != nil {
					errc <- fmt.Errorf("ping: %w", err)
					return
				}
			}
		}
	}()
	go func() { // reader
		for {
			_, b, err := c.Read(ctx)
			if err != nil {
				errc <- err
				return
			}
			var m proto.Envelope
			if json.Unmarshal(b, &m) != nil {
				continue
			}
			if m.Type == "req" {
				go a.handle(ctx, m)
			}
		}
	}()
	err = <-errc
	return err
}

func (a *Agent) queuePending(m proto.Envelope) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if len(a.pending) < 50 {
		a.pending = append(a.pending, m)
	}
}

// send delivers a message on the current connection. Job results are kept for the next
// connection when the agent is offline; other messages are dropped.
func (a *Agent) send(m proto.Envelope) {
	a.mu.Lock()
	out := a.out
	a.mu.Unlock()
	if out != nil {
		select {
		case out <- m:
			return
		default:
		}
	}
	if m.Type == "job" && m.Event == "done" {
		a.queuePending(m)
	}
}

func (a *Agent) capabilities() []string {
	caps := []string{"metrics", "selfupdate", "exec", "files"}
	if runtime.GOOS == "linux" {
		caps = append(caps, "reboot", "logs", "services", "packages")
		if apt.Available() {
			caps = append(caps, "apt")
		}
		if _, err := governor.Get(); err == nil {
			caps = append(caps, "governor")
		}
		if pve.Available() {
			caps = append(caps, "pve", "pve-notifications")
		}
	}
	return caps
}

func (a *Agent) hello(ctx context.Context) proto.Hello {
	h := proto.Hello{Protocol: proto.Version, AgentVersion: a.Version, OS: runtime.GOOS, Arch: runtime.GOARCH,
		Hostname: sysinfo.Hostname(), Info: sysinfo.Info(), Capabilities: a.capabilities(), Install: selfupdate.InstallKind()}
	if g, err := governor.Get(); err == nil {
		g.Desired = config.LoadState().Governor
		h.Governor = g
	}
	if pve.Available() {
		if cl, err := pve.ClusterStatus(ctx); err == nil {
			h.Info["cluster"] = cl
		}
	}
	return h
}

func (a *Agent) metrics(ctx context.Context) sysinfo.Metrics {
	m := sysinfo.Collect()
	if pve.Available() {
		a.updMu.Lock()
		stale := time.Since(a.guestsAt) > time.Minute
		a.updMu.Unlock()
		if stale {
			if g, err := pve.Guests(ctx); err == nil {
				a.updMu.Lock()
				a.guests, a.guestsAt = g, time.Now()
				a.updMu.Unlock()
			}
		}
		a.updMu.Lock()
		m.Guests = a.guests
		a.updMu.Unlock()
	}
	return m
}

func (a *Agent) tickers(ctx context.Context, out chan<- proto.Envelope) {
	a.send(proto.Envelope{Type: "metrics", Data: a.metrics(ctx)})
	a.updMu.Lock()
	if a.lastUpdates != nil {
		u := *a.lastUpdates
		a.updMu.Unlock()
		a.send(proto.Envelope{Type: "updates", Data: u})
	} else {
		a.updMu.Unlock()
	}
	mt := time.NewTicker(MetricsInterval)
	ct := time.NewTicker(ClusterInterval)
	defer mt.Stop()
	defer ct.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-mt.C:
			a.send(proto.Envelope{Type: "metrics", Data: a.metrics(ctx)})
		case <-ct.C:
			if pve.Available() {
				if cl, err := pve.ClusterStatus(ctx); err == nil {
					a.send(proto.Envelope{Type: "cluster", Data: cl})
				}
			}
		}
	}
}

func (a *Agent) checkUpdates(ctx context.Context, refresh bool) proto.Updates {
	u := apt.Check(ctx, refresh)
	a.updMu.Lock()
	a.lastUpdates = &u
	a.updMu.Unlock()
	a.send(proto.Envelope{Type: "updates", Data: u})
	return u
}

func (a *Agent) updatesLoop(ctx context.Context) {
	if !apt.Available() {
		return
	}
	select {
	case <-ctx.Done():
		return
	case <-time.After(20 * time.Second):
	}
	for {
		a.checkUpdates(ctx, true)
		select {
		case <-ctx.Done():
			return
		case <-time.After(UpdatesInterval):
		}
	}
}

// ---------------------------------------------------------------- requests

func (a *Agent) reply(m proto.Envelope, result any, err error) {
	r := proto.Envelope{Type: "res", ID: m.ID, OK: err == nil, Result: result}
	if err != nil {
		r.Error = err.Error()
	}
	a.send(r)
}

func (a *Agent) job(jobID string, fn func(line func(string)) (any, error)) {
	line := func(s string) { a.send(proto.Envelope{Type: "job", JobID: jobID, Event: "line", Line: s}) }
	res, err := fn(line)
	done := proto.Envelope{Type: "job", JobID: jobID, Event: "done", Status: "success", Result: res}
	if err != nil {
		done.Status, done.Error = "failed", err.Error()
	}
	a.send(done)
}

func (a *Agent) handle(ctx context.Context, m proto.Envelope) {
	var p map[string]json.RawMessage
	_ = json.Unmarshal(m.Params, &p)
	str := func(k string) string {
		var s string
		_ = json.Unmarshal(p[k], &s)
		return s
	}
	switch m.Method {
	case "ping":
		a.reply(m, map[string]any{"pong": true, "version": a.Version}, nil)
	case "info":
		a.reply(m, a.hello(ctx), nil)
	case "metrics":
		a.reply(m, a.metrics(ctx), nil)
	case "updates.check":
		a.reply(m, a.checkUpdates(ctx, true), nil)
	case "updates.apply":
		jobID := str("job_id")
		a.reply(m, map[string]any{"started": true}, nil)
		a.job(jobID, func(line func(string)) (any, error) {
			err := apt.Apply(context.Background(), line)
			u := a.checkUpdates(context.Background(), false)
			return map[string]any{"remaining": len(u.Packages), "reboot_required": u.RebootRequired}, err
		})
	case "governor.get":
		g, err := governor.Get()
		if g != nil {
			g.Desired = config.LoadState().Governor
		}
		a.reply(m, g, err)
	case "governor.set":
		gov := str("governor")
		g, err := governor.Set(gov)
		if err == nil {
			err = config.UpdateState(func(s *config.State) { s.Governor = gov })
			g.Desired = gov
			a.send(proto.Envelope{Type: "metrics", Data: a.metrics(ctx)})
		}
		a.reply(m, g, err)
	case "notifications.apply":
		var n pve.Notify
		if err := json.Unmarshal(m.Params, &n); err != nil {
			a.reply(m, nil, err)
			return
		}
		res, err := pve.ApplyNotify(ctx, n)
		a.reply(m, res, err)
	case "notifications.test":
		a.reply(m, map[string]any{"sent": true}, pve.TestNotify(ctx, str("target")))
	case "agent.update":
		var req selfupdate.Request
		if err := json.Unmarshal(m.Params, &req); err != nil {
			a.reply(m, nil, err)
			return
		}
		if strings.HasPrefix(req.URL, "/") {
			req.URL = "https://" + a.cfg.Server + req.URL
		}
		jobID := str("job_id")
		a.reply(m, map[string]any{"started": true}, nil)
		a.job(jobID, func(line func(string)) (any, error) {
			line("downloading " + req.Version)
			file, err := selfupdate.Download(a.http, req)
			if err != nil {
				return nil, err
			}
			line("checksum verified, installing")
			restart, err := selfupdate.Install(file, req.Format)
			if err != nil {
				return nil, err
			}
			line("installed; the agent restarts with the new version")
			if restart {
				go func() { time.Sleep(2 * time.Second); a.exit(0) }()
			}
			return map[string]any{"version": req.Version}, nil
		})
	case "exec":
		var q struct {
			Command string `json:"command"`
			Timeout int    `json:"timeout"`
		}
		_ = json.Unmarshal(m.Params, &q)
		r, err := ops.Exec(ctx, q.Command, q.Timeout)
		a.reply(m, r, err)
	case "logs.journal":
		var q ops.JournalQuery
		_ = json.Unmarshal(m.Params, &q)
		r, err := ops.Journal(ctx, q)
		a.reply(m, r, err)
	case "services.list":
		r, err := ops.Services(ctx, str("state"))
		a.reply(m, r, err)
	case "services.status":
		r, err := ops.ServiceStatus(ctx, str("unit"))
		a.reply(m, r, err)
	case "services.action":
		r, err := ops.ServiceAction(ctx, str("unit"), str("action"))
		a.reply(m, r, err)
	case "packages.query":
		r, err := ops.Packages(ctx, str("pattern"))
		a.reply(m, r, err)
	case "file.read":
		var q struct {
			Path     string `json:"path"`
			MaxBytes int    `json:"max_bytes"`
			Tail     bool   `json:"tail"`
		}
		_ = json.Unmarshal(m.Params, &q)
		r, err := ops.ReadFile(q.Path, q.MaxBytes, q.Tail)
		a.reply(m, r, err)
	case "reboot":
		if runtime.GOOS != "linux" {
			a.reply(m, nil, errors.New("reboot is not supported on this system yet"))
			return
		}
		a.reply(m, map[string]any{"rebooting": true}, nil)
		go func() {
			time.Sleep(3 * time.Second)
			_ = exec.Command("systemctl", "reboot").Run()
		}()
	case "unenroll":
		err := config.Remove()
		a.reply(m, map[string]any{"removed": err == nil}, err)
		go func() { time.Sleep(time.Second); a.exit(0) }()
	default:
		a.reply(m, nil, fmt.Errorf("unknown method %q", m.Method))
	}
}
