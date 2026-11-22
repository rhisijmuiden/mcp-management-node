// Package stream carries interactive sessions (VM console over VNC, container console, shells
// and interactive community scripts) between the node and the browser.
//
// For every session the agent opens a dedicated WebSocket to the app (/agent/stream/<id>, same
// mutual TLS as the control connection). Binary frames carry the raw bytes; text frames carry
// small JSON control messages ({"type":"resize","cols":..,"rows":..} from the browser,
// {"type":"exit","code":..} from the agent).
package stream

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"regexp"
	"strconv"
	"time"

	"github.com/coder/websocket"

	"github.com/rhisijmuiden/stackpilot/agent/internal/pve"
)

var sessionRE = regexp.MustCompile(`^[a-f0-9]{32}$`)

type Request struct {
	Session string            `json:"session"`
	Kind    string            `json:"kind"` // vnc | pty
	VMID    int               `json:"vmid"`
	Mode    string            `json:"mode"` // pty: lxc-console | lxc-shell | node-shell | script
	Script  string            `json:"script"`
	Env     map[string]string `json:"env"`
	Cols    int               `json:"cols"`
	Rows    int               `json:"rows"`
}

// Prepared holds what was set up before the app connects the browser.
type Prepared struct {
	Port     int    `json:"-"`
	Password string `json:"password,omitempty"`
}

func (r *Request) Validate() error {
	if !sessionRE.MatchString(r.Session) {
		return errors.New("invalid session id")
	}
	switch r.Kind {
	case "vnc":
		if r.VMID <= 0 {
			return errors.New("vmid is required")
		}
	case "pty":
		switch r.Mode {
		case "lxc-console", "lxc-shell":
			if r.VMID <= 0 {
				return errors.New("vmid is required")
			}
		case "node-shell":
		case "script":
			if r.Script == "" {
				return errors.New("script is required")
			}
		default:
			return fmt.Errorf("unknown terminal mode %q", r.Mode)
		}
	default:
		return fmt.Errorf("unknown stream kind %q", r.Kind)
	}
	return nil
}

// Prepare starts the Proxmox VE VNC proxy for a VM (a one-time password protects it).
func Prepare(ctx context.Context, r *Request) (*Prepared, error) {
	if err := r.Validate(); err != nil {
		return nil, err
	}
	if r.Kind != "vnc" {
		return &Prepared{}, nil
	}
	out, err := pve.API(ctx, "create", "/nodes/localhost/qemu/"+strconv.Itoa(r.VMID)+"/vncproxy",
		map[string]any{"websocket": true, "generate-password": true}, 30*time.Second)
	if err != nil {
		return nil, err
	}
	var res struct {
		Port     any    `json:"port"`
		Password string `json:"password"`
		Ticket   string `json:"ticket"`
	}
	if err := json.Unmarshal(out, &res); err != nil {
		return nil, err
	}
	port := 0
	switch p := res.Port.(type) {
	case float64:
		port = int(p)
	case string:
		port, _ = strconv.Atoi(p)
	}
	if port <= 0 {
		return nil, errors.New("Proxmox VE did not return a VNC port")
	}
	pw := res.Password
	if pw == "" {
		pw = res.Ticket
	}
	return &Prepared{Port: port, Password: pw}, nil
}

type control struct {
	Type string `json:"type"`
	Cols int    `json:"cols"`
	Rows int    `json:"rows"`
	Code int    `json:"code"`
}

// Serve pipes the session over the WebSocket until either side ends it.
func Serve(ctx context.Context, c *websocket.Conn, r *Request, p *Prepared) error {
	defer c.CloseNow()
	c.SetReadLimit(1 << 20)
	if r.Kind == "vnc" {
		return serveVNC(ctx, c, p)
	}
	return servePTY(ctx, c, r)
}

func sendExit(ctx context.Context, c *websocket.Conn, code int, msg string) {
	b, _ := json.Marshal(map[string]any{"type": "exit", "code": code, "message": msg})
	wctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	_ = c.Write(wctx, websocket.MessageText, b)
	_ = c.Close(websocket.StatusNormalClosure, "done")
}

func serveVNC(ctx context.Context, c *websocket.Conn, p *Prepared) error {
	var tcp net.Conn
	var err error
	for i := 0; i < 50; i++ { // the proxy needs a moment to listen
		tcp, err = net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(p.Port)), 2*time.Second)
		if err == nil {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if err != nil {
		sendExit(ctx, c, 1, "could not reach the VNC proxy: "+err.Error())
		return err
	}
	defer tcp.Close()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() {
		buf := make([]byte, 64*1024)
		for {
			n, err := tcp.Read(buf)
			if n > 0 {
				if werr := c.Write(ctx, websocket.MessageBinary, buf[:n]); werr != nil {
					cancel()
					return
				}
			}
			if err != nil {
				sendExit(ctx, c, 0, "console closed")
				cancel()
				return
			}
		}
	}()
	for {
		typ, data, err := c.Read(ctx)
		if err != nil {
			return nil
		}
		if typ == websocket.MessageBinary {
			if _, err := tcp.Write(data); err != nil {
				return err
			}
		}
	}
}
