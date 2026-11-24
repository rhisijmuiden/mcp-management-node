// Package client keeps the outgoing mTLS WebSocket connection to Stackpilot open and
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
	"sync/atomic"
	"time"

	"github.com/coder/websocket"

	"github.com/rhisijmuiden/stackpilot/agent/internal/apt"
	"github.com/rhisijmuiden/stackpilot/agent/internal/config"
	"github.com/rhisijmuiden/stackpilot/agent/internal/diag"
	"github.com/rhisijmuiden/stackpilot/agent/internal/governor"
	"github.com/rhisijmuiden/stackpilot/agent/internal/httprelay"
	"github.com/rhisijmuiden/stackpilot/agent/internal/logsearch"
	"github.com/rhisijmuiden/stackpilot/agent/internal/netwatch"
	"github.com/rhisijmuiden/stackpilot/agent/internal/ops"
	"github.com/rhisijmuiden/stackpilot/agent/internal/pkgmgr"
	"github.com/rhisijmuiden/stackpilot/agent/internal/proto"
	"github.com/rhisijmuiden/stackpilot/agent/internal/pve"
	"github.com/rhisijmuiden/stackpilot/agent/internal/script"
	"github.com/rhisijmuiden/stackpilot/agent/internal/selfupdate"
	"github.com/rhisijmuiden/stackpilot/agent/internal/stream"
	"github.com/rhisijmuiden/stackpilot/agent/internal/sysinfo"
	"github.com/rhisijmuiden/stackpilot/agent/internal/tlsutil"
	"github.com/rhisijmuiden/stackpilot/agent/internal/transfer"
)

// RestartExitCode is the exit code after a self-update; the service manager starts the new
// version (systemd: Restart=always; Windows: recovery on non-zero exit).
var RestartExitCode = 0

// BeforeRestart, when set, runs just before the agent exits to restart after a self-update.
// On Windows it arms a scheduled task that starts the service should the service recovery not.
var BeforeRestart func()

// DrainTimeout is how long a self-update waits for running requests (commands, scripts,
// file transfers) to finish before the agent restarts.
var DrainTimeout = 5 * time.Minute

// restartDelay gives the update job's result time to reach the app before the restart.
var restartDelay = 2 * time.Second

var (
	MetricsInterval = 15 * time.Second
	UpdatesInterval = 6 * time.Hour
	ClusterInterval = 5 * time.Minute
	GuestsInterval  = 20 * time.Second
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
	failedSvc   *int
	failedAt    time.Time

	exit     func(int)
	inflight atomic.Int64 // requests being handled right now

	offlineSince atomic.Int64 // unix nanoseconds since the last connection ended (0 = connected)
	net          *netwatch.Watcher
}

// OfflineFor is how long the agent has been without a connection to Stackpilot.
func (a *Agent) OfflineFor() time.Duration {
	since := a.offlineSince.Load()
	if since == 0 {
		return 0
	}
	return time.Since(time.Unix(0, since))
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
	a := &Agent{
		Version: version, cfg: cfg, tls: tc,
		http: &http.Client{Timeout: 10 * time.Minute, Transport: &http.Transport{TLSClientConfig: tc, Proxy: http.ProxyFromEnvironment}},
		exit: os.Exit,
	}
	a.offlineSince.Store(time.Now().UnixNano())
	a.net = netwatch.New(a.OfflineFor)
	return a, nil
}

// Run connects forever, with exponential backoff (1s .. 60s, jittered).
func (a *Agent) Run(ctx context.Context) error {
	a.applyStoredGovernor()
	go a.updatesLoop(ctx)
	if a.net != nil {
		go a.net.Run(ctx)
	}
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
		log.Printf("disconnected from %s: %v (retry in %s)", a.cfg.Server, tlsutil.Explain(a.cfg.Server, err), backoff)
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
	c, err := a.dial(ctx, "/agent/ws")
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
	a.offlineSince.Store(0)
	defer func() {
		a.mu.Lock()
		a.out = nil
		a.mu.Unlock()
		a.offlineSince.Store(time.Now().UnixNano())
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
				a.inflight.Add(1)
				go func() {
					defer a.inflight.Add(-1)
					a.handle(ctx, m)
				}()
			}
		}
	}()
	err = <-errc
	return err
}

// dial opens a WebSocket to the app with the agent's client certificate.
func (a *Agent) dial(ctx context.Context, path string) (*websocket.Conn, error) {
	hdr := http.Header{}
	hdr.Set("User-Agent", "mmn-agent/"+a.Version)
	c, _, err := websocket.Dial(ctx, "wss://"+a.cfg.Server+path, &websocket.DialOptions{
		HTTPClient: &http.Client{Timeout: 30 * time.Second, Transport: &http.Transport{TLSClientConfig: a.tls, Proxy: http.ProxyFromEnvironment}},
		HTTPHeader: hdr,
	})
	return c, err
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
	caps := []string{"metrics", "selfupdate", "exec", "files", "logsearch", "diagnose", "copy", "http", "netwatch"}
	if pkgmgr.Available() {
		caps = append(caps, "updates", "update-"+pkgmgr.Name())
	}
	switch runtime.GOOS {
	case "windows":
		caps = append(caps, "reboot", "logs", "eventlog", "services", "packages", "powershell", "terminal", "scripts",
			"ps-scripts")
		if apt.Available() {
			caps = append(caps, "apt")
		}
	case "linux":
		caps = append(caps, "reboot", "logs", "services", "packages")
		if apt.Available() {
			caps = append(caps, "apt")
		}
		if _, err := governor.Get(); err == nil {
			caps = append(caps, "governor")
		}
		caps = append(caps, "terminal", "scripts")
		if pve.Available() {
			caps = append(caps, "pve", "pve-notifications", "guests", "console", "pve-api")
		}
	}
	return caps
}

func (a *Agent) hello(ctx context.Context) proto.Hello {
	h := proto.Hello{Protocol: proto.Version, AgentVersion: a.Version, OS: runtime.GOOS, Arch: runtime.GOARCH,
		Hostname: sysinfo.Hostname(), Info: sysinfo.Info(), Capabilities: a.capabilities(), Install: selfupdate.InstallKind()}
	if n := pkgmgr.Name(); n != "" {
		h.Info["update_manager"] = n
	}
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
	// Failed services: refreshed every 2 minutes (a PowerShell query on Windows is not free).
	a.updMu.Lock()
	stale := time.Since(a.failedAt) > 2*time.Minute
	a.updMu.Unlock()
	if stale {
		if n, err := ops.FailedServiceCount(ctx); err == nil {
			a.updMu.Lock()
			a.failedSvc, a.failedAt = &n, time.Now()
			a.updMu.Unlock()
		} else {
			a.updMu.Lock()
			a.failedAt = time.Now()
			a.updMu.Unlock()
		}
	}
	a.updMu.Lock()
	m.FailedServices = a.failedSvc
	a.updMu.Unlock()
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
	gt := time.NewTicker(GuestsInterval)
	defer mt.Stop()
	defer ct.Stop()
	defer gt.Stop()
	a.sendGuests(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		case <-mt.C:
			a.send(proto.Envelope{Type: "metrics", Data: a.metrics(ctx)})
		case <-gt.C:
			a.sendGuests(ctx)
		case <-ct.C:
			if pve.Available() {
				if cl, err := pve.ClusterStatus(ctx); err == nil {
					a.send(proto.Envelope{Type: "cluster", Data: cl})
				}
			}
		}
	}
}

func (a *Agent) sendGuests(ctx context.Context) *pve.GuestList {
	if !pve.Available() {
		return nil
	}
	gl, err := pve.ListGuests(ctx)
	if err != nil {
		log.Printf("guest list: %v", err)
		return nil
	}
	a.send(proto.Envelope{Type: "guests", Data: gl})
	return gl
}

func (a *Agent) checkUpdates(ctx context.Context, refresh bool) proto.Updates {
	u := pkgmgr.Check(ctx, refresh)
	a.updMu.Lock()
	a.lastUpdates = &u
	a.updMu.Unlock()
	a.send(proto.Envelope{Type: "updates", Data: u})
	return u
}

func (a *Agent) updatesLoop(ctx context.Context) {
	if !pkgmgr.Available() {
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

// restartAfterUpdate waits until the other requests are done (the update request itself is
// still in flight), then restarts into the new version.
func (a *Agent) restartAfterUpdate(version string) {
	time.Sleep(restartDelay)
	if !a.waitIdle(1, DrainTimeout) {
		log.Printf("update to %s: %d request(s) still running after %s, restarting anyway", version, a.inflight.Load()-1, DrainTimeout)
	}
	log.Printf("restarting to run version %s", version)
	if BeforeRestart != nil {
		BeforeRestart()
	}
	a.exit(RestartExitCode)
}

// waitIdle waits until at most max requests are in flight, or the timeout passes.
func (a *Agent) waitIdle(max int64, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for a.inflight.Load() > max {
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(200 * time.Millisecond)
	}
	return true
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
			err := pkgmgr.Apply(context.Background(), line)
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
		log.Printf("update to %s requested", req.Version)
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
				go a.restartAfterUpdate(req.Version)
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
	case "guests.list":
		gl := a.sendGuests(ctx)
		if gl == nil {
			a.reply(m, nil, errors.New("guests are only available on a Proxmox VE node"))
			return
		}
		a.reply(m, gl, nil)
	case "pve.api":
		var q struct {
			Method  string         `json:"method"`
			Path    string         `json:"path"`
			Args    map[string]any `json:"args"`
			Timeout int            `json:"timeout"`
		}
		if err := json.Unmarshal(m.Params, &q); err != nil {
			a.reply(m, nil, err)
			return
		}
		out, err := pve.API(ctx, q.Method, q.Path, q.Args, time.Duration(q.Timeout)*time.Second)
		a.reply(m, out, err)
	case "pve.task":
		var q struct {
			Method string         `json:"method"`
			Path   string         `json:"path"`
			Args   map[string]any `json:"args"`
		}
		if err := json.Unmarshal(m.Params, &q); err != nil {
			a.reply(m, nil, err)
			return
		}
		a.reply(m, map[string]any{"started": true}, nil)
		a.job(str("job_id"), func(line func(string)) (any, error) {
			res, err := pve.RunTask(context.Background(), q.Method, q.Path, q.Args, line)
			a.sendGuests(context.Background())
			return res, err
		})
	case "script.run":
		var q struct {
			Script  string            `json:"script"`
			Env     map[string]string `json:"env"`
			Timeout int               `json:"timeout"`
		}
		if err := json.Unmarshal(m.Params, &q); err != nil {
			a.reply(m, nil, err)
			return
		}
		a.reply(m, map[string]any{"started": true}, nil)
		a.job(str("job_id"), func(line func(string)) (any, error) {
			r, err := script.Run(context.Background(), q.Script, q.Env, time.Duration(q.Timeout)*time.Second, line)
			a.sendGuests(context.Background())
			return r, err
		})
	case "netwatch.status", "netwatch.apply", "netwatch.ack":
		if a.net == nil {
			a.reply(m, nil, errors.New("the network watchdog is not running"))
			return
		}
		a.netwatch(m)
	case "file.fetch":
		var q fetchRequest
		if err := json.Unmarshal(m.Params, &q); err != nil {
			a.reply(m, nil, err)
			return
		}
		if strings.HasPrefix(q.URL, "/") {
			q.URL = "https://" + a.cfg.Server + q.URL
		}
		a.reply(m, map[string]any{"started": true}, nil)
		a.job(str("job_id"), func(line func(string)) (any, error) {
			return fetchFile(context.Background(), a.http, q, line)
		})
	case "stream.open":
		var q stream.Request
		if err := json.Unmarshal(m.Params, &q); err != nil {
			a.reply(m, nil, err)
			return
		}
		prep, err := stream.Prepare(ctx, &q)
		if err != nil {
			a.reply(m, nil, err)
			return
		}
		a.reply(m, prep, nil)
		go func() {
			sctx, cancel := context.WithTimeout(context.Background(), 12*time.Hour)
			defer cancel()
			c, err := a.dial(sctx, "/agent/stream/"+q.Session)
			if err != nil {
				log.Printf("stream %s: %v", q.Session, err)
				return
			}
			if err := stream.Serve(sctx, c, &q, prep); err != nil {
				log.Printf("stream %s ended: %v", q.Session, err)
			}
		}()
	case "reboot":
		switch runtime.GOOS {
		case "linux":
			a.reply(m, map[string]any{"rebooting": true}, nil)
			go func() {
				time.Sleep(3 * time.Second)
				_ = exec.Command("systemctl", "reboot").Run()
			}()
		case "windows":
			a.reply(m, map[string]any{"rebooting": true}, nil)
			go func() {
				time.Sleep(3 * time.Second)
				_ = exec.Command("shutdown.exe", "/r", "/t", "10", "/d", "p:0:0", "/c", "Restart requested from Stackpilot").Run()
			}()
		default:
			a.reply(m, nil, errors.New("reboot is not supported on this system"))
		}
	case "logs.events":
		var q ops.JournalQuery
		_ = json.Unmarshal(m.Params, &q)
		r, err := ops.Events(ctx, q)
		a.reply(m, r, err)
	case "logs.search":
		var q logsearch.Query
		if err := json.Unmarshal(m.Params, &q); err != nil {
			a.reply(m, nil, err)
			return
		}
		r, err := logsearch.Search(ctx, q)
		a.reply(m, r, err)
	case "diagnose":
		r := diag.Run(ctx)
		a.updMu.Lock()
		if a.lastUpdates != nil {
			if n := len(a.lastUpdates.Packages); n > 0 {
				r.Findings = append(r.Findings, fmt.Sprintf("%d updates pending", n))
			}
			if a.lastUpdates.RebootRequired {
				r.Findings = append(r.Findings, "a reboot is required to finish updates")
			}
		}
		a.updMu.Unlock()
		a.reply(m, r, nil)
	case "copy.send", "copy.receive":
		var q struct {
			URL       string   `json:"url"`
			Paths     []string `json:"paths"`
			Dest      string   `json:"dest"`
			Overwrite bool     `json:"overwrite"`
		}
		if err := json.Unmarshal(m.Params, &q); err != nil {
			a.reply(m, nil, err)
			return
		}
		if !strings.HasPrefix(q.URL, "/agent/transfer/") {
			a.reply(m, nil, errors.New("invalid transfer address"))
			return
		}
		url := "https://" + a.cfg.Server + q.URL
		a.reply(m, map[string]any{"started": true}, nil)
		a.job(str("job_id"), func(line func(string)) (any, error) {
			hc := &http.Client{Transport: &http.Transport{TLSClientConfig: a.tls, Proxy: http.ProxyFromEnvironment}}
			if m.Method == "copy.send" {
				return transfer.Send(context.Background(), hc, url, q.Paths, line)
			}
			return transfer.Receive(context.Background(), hc, url, q.Dest, q.Overwrite, line)
		})
	case "unenroll":
		err := config.Remove()
		a.reply(m, map[string]any{"removed": err == nil}, err)
		go func() { time.Sleep(time.Second); a.exit(0) }()
	case "http.request":
		var q httprelay.Request
		if err := json.Unmarshal(m.Params, &q); err != nil {
			a.reply(m, nil, err)
			return
		}
		res, err := httprelay.Do(ctx, q)
		a.reply(m, res, err)
	default:
		a.reply(m, nil, fmt.Errorf("unknown method %q", m.Method))
	}
}

func (a *Agent) netwatch(m proto.Envelope) {
	switch m.Method {
	case "netwatch.status":
		a.reply(m, a.net.Status(), nil)
	case "netwatch.apply":
		var c netwatch.Config
		if err := json.Unmarshal(m.Params, &c); err != nil {
			a.reply(m, nil, err)
			return
		}
		a.reply(m, a.net.Apply(c), nil)
	case "netwatch.ack":
		var q struct {
			Until time.Time `json:"until"`
		}
		if err := json.Unmarshal(m.Params, &q); err != nil {
			a.reply(m, nil, err)
			return
		}
		a.reply(m, map[string]any{"removed": a.net.Ack(q.Until)}, nil)
	}
}
