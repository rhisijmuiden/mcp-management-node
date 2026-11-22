package pve

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Generic access to the Proxmox VE API for the app. The app decides which calls are allowed
// (it checks permissions and writes the audit log); the agent only validates the shape.

var (
	pathRE   = regexp.MustCompile(`^/[A-Za-z0-9/_.:@%=+-]{0,400}$`)
	argKeyRE = regexp.MustCompile(`^[a-z][a-z0-9_-]{0,63}$`)
)

func methodOK(m string) bool {
	switch m {
	case "get", "create", "set", "delete":
		return true
	}
	return false
}

// buildArgs turns {"memory": 2048, "start": true, "net0": "..."} into pvesh flags. Lists become
// repeated flags. Keys are sorted so calls are reproducible (and testable).
func buildArgs(args map[string]any) ([]string, error) {
	keys := make([]string, 0, len(args))
	for k := range args {
		if !argKeyRE.MatchString(k) {
			return nil, fmt.Errorf("invalid parameter name %q", k)
		}
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := []string{}
	add := func(k string, v any) error {
		switch x := v.(type) {
		case nil:
			return nil
		case bool:
			if x {
				out = append(out, "--"+k, "1")
			} else {
				out = append(out, "--"+k, "0")
			}
		case float64:
			out = append(out, "--"+k, strconv.FormatFloat(x, 'f', -1, 64))
		case int:
			out = append(out, "--"+k, strconv.Itoa(x))
		case string:
			out = append(out, "--"+k, x)
		default:
			return fmt.Errorf("unsupported value for %q", k)
		}
		return nil
	}
	for _, k := range keys {
		if list, ok := args[k].([]any); ok {
			for _, v := range list {
				if err := add(k, v); err != nil {
					return nil, err
				}
			}
			continue
		}
		if err := add(k, args[k]); err != nil {
			return nil, err
		}
	}
	return out, nil
}

// API runs one pvesh call and returns its JSON output.
func API(ctx context.Context, method, path string, args map[string]any, timeout time.Duration) (json.RawMessage, error) {
	if !Available() {
		return nil, errors.New("pvesh not found: this is not a Proxmox VE node")
	}
	if !methodOK(method) {
		return nil, errors.New("method must be get, create, set or delete")
	}
	if !pathRE.MatchString(path) || strings.Contains(path, "..") {
		return nil, errors.New("invalid API path")
	}
	flags, err := buildArgs(args)
	if err != nil {
		return nil, err
	}
	if timeout <= 0 || timeout > 10*time.Minute {
		timeout = 60 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	out, err := Pvesh(ctx, append([]string{method, path, "--output-format", "json"}, flags...)...)
	if err != nil {
		return nil, err
	}
	trimmed := strings.TrimSpace(string(out))
	if trimmed == "" {
		return json.RawMessage("null"), nil
	}
	if !json.Valid([]byte(trimmed)) {
		// Some calls print plain text (e.g. a UPID without quotes on older versions).
		b, _ := json.Marshal(trimmed)
		return b, nil
	}
	return json.RawMessage(trimmed), nil
}

// LocalNodeName is the Proxmox VE name of this node (the short host name, as pve uses it).
func LocalNodeName(ctx context.Context) string {
	if c, err := ClusterStatus(ctx); err == nil {
		for _, n := range c.Nodes {
			if n.Local && n.Name != "" {
				return n.Name
			}
		}
	}
	h, _ := os.Hostname()
	return strings.SplitN(h, ".", 2)[0]
}

// Guest is one VM or container as the app shows it.
type Guest struct {
	VMID     int     `json:"vmid"`
	Type     string  `json:"type"` // qemu | lxc
	Name     string  `json:"name"`
	Node     string  `json:"node"`
	Status   string  `json:"status"`
	CPU      float64 `json:"cpu"`
	MaxCPU   float64 `json:"maxcpu"`
	Mem      int64   `json:"mem"`
	MaxMem   int64   `json:"maxmem"`
	Disk     int64   `json:"disk"`
	MaxDisk  int64   `json:"maxdisk"`
	Uptime   int64   `json:"uptime"`
	NetIn    int64   `json:"netin"`
	NetOut   int64   `json:"netout"`
	Tags     string  `json:"tags,omitempty"`
	Template bool    `json:"template,omitempty"`
	Lock     string  `json:"lock,omitempty"`
	HAState  string  `json:"hastate,omitempty"`
	Pool     string  `json:"pool,omitempty"`
}

type GuestList struct {
	Node   string  `json:"node"`
	At     string  `json:"at"`
	Guests []Guest `json:"guests"`
}

// ListGuests returns the guests running on (or assigned to) this node.
func ListGuests(ctx context.Context) (*GuestList, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	local := LocalNodeName(ctx)
	var raw []map[string]any
	if err := getJSON(ctx, "/cluster/resources", &raw); err != nil {
		return nil, err
	}
	gl := &GuestList{Node: local, At: time.Now().UTC().Format(time.RFC3339), Guests: []Guest{}}
	for _, r := range raw {
		typ, _ := r["type"].(string)
		if typ != "qemu" && typ != "lxc" {
			continue
		}
		node, _ := r["node"].(string)
		if node != local {
			continue
		}
		g := Guest{Type: typ, Node: node, VMID: int(num(r["vmid"])), CPU: num(r["cpu"]), MaxCPU: num(r["maxcpu"]),
			Mem: int64(num(r["mem"])), MaxMem: int64(num(r["maxmem"])), Disk: int64(num(r["disk"])),
			MaxDisk: int64(num(r["maxdisk"])), Uptime: int64(num(r["uptime"])), NetIn: int64(num(r["netin"])),
			NetOut: int64(num(r["netout"])), Template: truthy(r["template"])}
		g.Name, _ = r["name"].(string)
		g.Status, _ = r["status"].(string)
		g.Tags, _ = r["tags"].(string)
		g.Lock, _ = r["lock"].(string)
		g.HAState, _ = r["hastate"].(string)
		g.Pool, _ = r["pool"].(string)
		gl.Guests = append(gl.Guests, g)
	}
	sort.Slice(gl.Guests, func(i, j int) bool { return gl.Guests[i].VMID < gl.Guests[j].VMID })
	return gl, nil
}

func num(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case string:
		f, _ := strconv.ParseFloat(x, 64)
		return f
	case json.Number:
		f, _ := x.Float64()
		return f
	}
	return 0
}

// ---------------------------------------------------------------- tasks

// FollowTask streams the log of a Proxmox VE task (UPID) until it stops and reports its result.
func FollowTask(ctx context.Context, upid string, line func(string)) error {
	parts := strings.Split(upid, ":")
	if len(parts) < 3 || parts[0] != "UPID" {
		return fmt.Errorf("not a task id: %q", upid)
	}
	base := "/nodes/" + parts[1] + "/tasks/" + upid
	start := 0
	deadline := time.Now().Add(6 * time.Hour)
	for {
		var logLines []struct {
			N int    `json:"n"`
			T string `json:"t"`
		}
		if out, err := API(ctx, "get", base+"/log", map[string]any{"start": start, "limit": 500}, 30*time.Second); err == nil {
			_ = json.Unmarshal(out, &logLines)
			for _, l := range logLines {
				if l.N > start {
					start = l.N
				}
				if l.T != "no content" {
					line(l.T)
				}
			}
		}
		var st struct {
			Status     string `json:"status"`
			ExitStatus string `json:"exitstatus"`
		}
		out, err := API(ctx, "get", base+"/status", nil, 30*time.Second)
		if err != nil {
			return err
		}
		_ = json.Unmarshal(out, &st)
		if st.Status == "stopped" && len(logLines) < 500 {
			if st.ExitStatus == "OK" || strings.HasPrefix(st.ExitStatus, "WARNINGS") {
				return nil
			}
			return fmt.Errorf("task failed: %s", st.ExitStatus)
		}
		if time.Now().After(deadline) {
			return errors.New("task still running after 6 hours")
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Second):
		}
	}
}

// RunTask starts an asynchronous API call (which returns a UPID) and follows it. Calls that
// finish at once (no UPID) succeed immediately with their output.
func RunTask(ctx context.Context, method, path string, args map[string]any, line func(string)) (map[string]any, error) {
	out, err := API(ctx, method, path, args, 5*time.Minute)
	if err != nil {
		return nil, err
	}
	var upid string
	if json.Unmarshal(out, &upid) != nil || !strings.HasPrefix(upid, "UPID:") {
		return map[string]any{"result": out}, nil
	}
	line("task " + upid)
	return map[string]any{"upid": upid}, FollowTask(ctx, upid, line)
}
