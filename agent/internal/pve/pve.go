// Package pve talks to the local Proxmox VE API through pvesh (as root, no API token needed).
package pve

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/rhisijmuiden/mcp-management-node/agent/internal/sysinfo"
)

// Pvesh runs pvesh; replaced in tests.
var Pvesh = func(ctx context.Context, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, "pvesh", args...)
	out, err := cmd.Output()
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			return out, fmt.Errorf("pvesh %s: %s", strings.Join(args[:min(2, len(args))], " "), strings.TrimSpace(string(ee.Stderr)))
		}
		return out, err
	}
	return out, nil
}

// Available reports whether pvesh exists; replaced in tests.
var Available = func() bool {
	_, err := exec.LookPath("pvesh")
	return err == nil
}

func getJSON(ctx context.Context, path string, v any) error {
	out, err := Pvesh(ctx, "get", path, "--output-format", "json")
	if err != nil {
		return err
	}
	return json.Unmarshal(out, v)
}

// Guests counts VMs and containers on this node.
func Guests(ctx context.Context) (*sysinfo.Guests, error) {
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	type guest struct {
		Status string `json:"status"`
	}
	var vms, cts []guest
	if err := getJSON(ctx, "/nodes/localhost/qemu", &vms); err != nil {
		return nil, err
	}
	if err := getJSON(ctx, "/nodes/localhost/lxc", &cts); err != nil {
		return nil, err
	}
	g := &sysinfo.Guests{VMsTotal: len(vms), CTsTotal: len(cts)}
	for _, v := range vms {
		if v.Status == "running" {
			g.VMsRunning++
		}
	}
	for _, c := range cts {
		if c.Status == "running" {
			g.CTsRunning++
		}
	}
	return g, nil
}

type ClusterNode struct {
	Name   string `json:"name"`
	Online bool   `json:"online"`
	Local  bool   `json:"local"`
	IP     string `json:"ip,omitempty"`
}

type Cluster struct {
	Name    string        `json:"name,omitempty"`
	Quorate bool          `json:"quorate"`
	Nodes   []ClusterNode `json:"nodes"`
}

// ClusterStatus describes the cluster this node belongs to (Name is empty for a single node).
func ClusterStatus(ctx context.Context) (*Cluster, error) {
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	var raw []map[string]any
	if err := getJSON(ctx, "/cluster/status", &raw); err != nil {
		return nil, err
	}
	c := &Cluster{Quorate: true, Nodes: []ClusterNode{}}
	for _, e := range raw {
		switch e["type"] {
		case "cluster":
			c.Name, _ = e["name"].(string)
			c.Quorate = truthy(e["quorate"])
		case "node":
			n := ClusterNode{Online: truthy(e["online"]), Local: truthy(e["local"])}
			n.Name, _ = e["name"].(string)
			n.IP, _ = e["ip"].(string)
			c.Nodes = append(c.Nodes, n)
		}
	}
	return c, nil
}

func truthy(v any) bool {
	switch x := v.(type) {
	case bool:
		return x
	case float64:
		return x != 0
	case string:
		return x == "1" || x == "true"
	}
	return false
}

// ---------------------------------------------------------------- notifications

// Notification settings pushed by the app (one SMTP target plus one matcher per type).
type Notify struct {
	Name        string   `json:"name"` // target name, default "mmn-smtp"
	Server      string   `json:"server"`
	Port        int      `json:"port"`
	Mode        string   `json:"mode"` // insecure | starttls | tls
	Username    string   `json:"username"`
	Password    string   `json:"password"`
	FromAddress string   `json:"from_address"`
	Author      string   `json:"author"`
	Mailto      []string `json:"mailto"`
	Types       []string `json:"types"`      // vzdump, replication, fencing, package-updates, system-mail
	Severities  []string `json:"severities"` // info, notice, warning, error, unknown
	Enabled     bool     `json:"enabled"`
}

var KnownTypes = []string{"vzdump", "replication", "fencing", "package-updates", "system-mail"}

const matcherPrefix = "mmn-"

func (n *Notify) target() string {
	if n.Name == "" {
		return "mmn-smtp"
	}
	return n.Name
}

func exists(ctx context.Context, path string) bool {
	_, err := Pvesh(ctx, "get", path, "--output-format", "json")
	return err == nil
}

// ApplyNotify creates or updates the SMTP target and the matchers. Disabled = remove them.
func ApplyNotify(ctx context.Context, n Notify) (map[string]any, error) {
	if !Available() {
		return nil, errors.New("pvesh not found: this is not a Proxmox VE node")
	}
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	if !exists(ctx, "/cluster/notifications/endpoints") {
		return nil, errors.New("this Proxmox VE version has no notification system (needs 8.1 or newer)")
	}
	if !n.Enabled {
		return map[string]any{"removed": true}, RemoveNotify(ctx, n.target())
	}
	if n.Server == "" || n.FromAddress == "" || len(n.Mailto) == 0 {
		return nil, errors.New("server, from address and at least one recipient are required")
	}
	mode := n.Mode
	if mode == "" {
		mode = "starttls"
	}
	path := "/cluster/notifications/endpoints/smtp"
	args := []string{"--server", n.Server, "--from-address", n.FromAddress, "--mode", mode,
		"--comment", "Managed by Stackpilot"}
	if n.Port > 0 {
		args = append(args, "--port", strconv.Itoa(n.Port))
	}
	if n.Author != "" {
		args = append(args, "--author", n.Author)
	}
	for _, m := range n.Mailto {
		args = append(args, "--mailto", m)
	}
	var del []string
	if n.Username != "" {
		args = append(args, "--username", n.Username)
		if n.Password != "" {
			args = append(args, "--password", n.Password)
		}
	} else {
		del = append(del, "username", "password")
	}
	if exists(ctx, path+"/"+n.target()) {
		if len(del) > 0 {
			args = append(args, "--delete", strings.Join(del, ","))
		}
		if _, err := Pvesh(ctx, append([]string{"set", path + "/" + n.target()}, args...)...); err != nil {
			return nil, err
		}
	} else if _, err := Pvesh(ctx, append([]string{"create", path, "--name", n.target()}, args...)...); err != nil {
		return nil, err
	}
	// One matcher per notification type, so "type is X AND severity in Y" holds.
	sev := strings.Join(n.Severities, ",")
	for _, t := range KnownTypes {
		name := matcherPrefix + t
		mpath := "/cluster/notifications/matchers/" + name
		if !slices.Contains(n.Types, t) {
			if exists(ctx, mpath) {
				if _, err := Pvesh(ctx, "delete", mpath); err != nil {
					return nil, err
				}
			}
			continue
		}
		margs := []string{"--target", n.target(), "--mode", "all", "--match-field", "exact:type=" + t,
			"--comment", "Managed by Stackpilot"}
		if sev != "" {
			margs = append(margs, "--match-severity", sev)
		}
		if exists(ctx, mpath) {
			if sev == "" {
				margs = append(margs, "--delete", "match-severity")
			}
			if _, err := Pvesh(ctx, append([]string{"set", mpath}, margs...)...); err != nil {
				return nil, err
			}
		} else if _, err := Pvesh(ctx, append([]string{"create", "/cluster/notifications/matchers", "--name", name}, margs...)...); err != nil {
			return nil, err
		}
	}
	return map[string]any{"target": n.target(), "types": n.Types}, nil
}

// RemoveNotify deletes everything this agent created.
func RemoveNotify(ctx context.Context, target string) error {
	for _, t := range KnownTypes {
		p := "/cluster/notifications/matchers/" + matcherPrefix + t
		if exists(ctx, p) {
			if _, err := Pvesh(ctx, "delete", p); err != nil {
				return err
			}
		}
	}
	p := "/cluster/notifications/endpoints/smtp/" + target
	if exists(ctx, p) {
		if _, err := Pvesh(ctx, "delete", p); err != nil {
			return err
		}
	}
	return nil
}

// TestNotify sends a test notification through the target.
func TestNotify(ctx context.Context, target string) error {
	if target == "" {
		target = "mmn-smtp"
	}
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	_, err := Pvesh(ctx, "create", "/cluster/notifications/targets/"+target+"/test")
	return err
}
