// Package ops gives the app (and through its MCP server, Claude) the everyday server-admin
// tools: logs, services, installed packages, files and commands. Every call is started by the
// app, which checks permissions and writes it to the audit log before it reaches the agent.
package ops

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"
)

const maxOutput = 256 * 1024

var unitRE = regexp.MustCompile(`^[A-Za-z0-9@._:\\-]{1,200}$`)

type ExecResult struct {
	ExitCode   int    `json:"exit_code"`
	Stdout     string `json:"stdout"`
	Stderr     string `json:"stderr"`
	Truncated  bool   `json:"truncated"`
	DurationMs int64  `json:"duration_ms"`
	TimedOut   bool   `json:"timed_out"`
}

type limitedBuf struct {
	b         bytes.Buffer
	truncated bool
}

func (l *limitedBuf) Write(p []byte) (int, error) {
	if room := maxOutput - l.b.Len(); room > 0 {
		if len(p) > room {
			l.b.Write(p[:room])
			l.truncated = true
		} else {
			l.b.Write(p)
		}
	} else {
		l.truncated = true
	}
	return len(p), nil
}

func run(ctx context.Context, timeout time.Duration, name string, args ...string) ExecResult {
	if timeout <= 0 || timeout > 30*time.Minute {
		timeout = 60 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	start := time.Now()
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Env = append(os.Environ(), "LC_ALL=C.UTF-8", "SYSTEMD_PAGER=", "PAGER=cat", "DEBIAN_FRONTEND=noninteractive")
	var out, errb limitedBuf
	cmd.Stdout, cmd.Stderr = &out, &errb
	err := cmd.Run()
	r := ExecResult{Stdout: out.b.String(), Stderr: errb.b.String(), Truncated: out.truncated || errb.truncated,
		DurationMs: time.Since(start).Milliseconds()}
	var ee *exec.ExitError
	switch {
	case errors.As(err, &ee):
		r.ExitCode = ee.ExitCode()
	case err != nil:
		r.ExitCode = -1
		r.Stderr += err.Error()
	}
	if ctx.Err() == context.DeadlineExceeded {
		r.TimedOut = true
	}
	return r
}

// Exec runs a shell command (as root). Only reachable with the app's "exec" permission.
func Exec(ctx context.Context, command string, timeoutSec int) (ExecResult, error) {
	if strings.TrimSpace(command) == "" {
		return ExecResult{}, errors.New("empty command")
	}
	if runtime.GOOS == "windows" {
		return run(ctx, time.Duration(timeoutSec)*time.Second, "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command), nil
	}
	return run(ctx, time.Duration(timeoutSec)*time.Second, "/bin/sh", "-c", command), nil
}

type JournalQuery struct {
	Unit     string `json:"unit"`
	Since    string `json:"since"` // "1 hour ago", "2026-11-22 10:00"
	Until    string `json:"until"`
	Lines    int    `json:"lines"`    // default 200, max 5000
	Priority string `json:"priority"` // emerg..debug, e.g. "err"
	Grep     string `json:"grep"`
	Boot     bool   `json:"boot"` // only this boot
}

var safeArg = regexp.MustCompile(`^[A-Za-z0-9 :._+\-/]{0,64}$`)

// Journal reads the systemd journal.
func Journal(ctx context.Context, q JournalQuery) (ExecResult, error) {
	if q.Lines <= 0 {
		q.Lines = 200
	}
	q.Lines = min(q.Lines, 5000)
	args := []string{"--no-pager", "-o", "short-iso", "-n", strconv.Itoa(q.Lines)}
	if q.Unit != "" {
		if !unitRE.MatchString(q.Unit) {
			return ExecResult{}, errors.New("invalid unit name")
		}
		args = append(args, "-u", q.Unit)
	}
	for flag, v := range map[string]string{"--since": q.Since, "--until": q.Until, "-p": q.Priority} {
		if v == "" {
			continue
		}
		if !safeArg.MatchString(v) {
			return ExecResult{}, fmt.Errorf("invalid value for %s", flag)
		}
		args = append(args, flag, v)
	}
	if q.Grep != "" {
		if len(q.Grep) > 200 {
			return ExecResult{}, errors.New("grep pattern too long")
		}
		args = append(args, "-g", q.Grep, "--case-sensitive=false")
	}
	if q.Boot {
		args = append(args, "-b")
	}
	return run(ctx, 60*time.Second, "journalctl", args...), nil
}

type Service struct {
	Name        string `json:"name"`
	Load        string `json:"load"`
	Active      string `json:"active"`
	Sub         string `json:"sub"`
	Description string `json:"description"`
}

// Services lists systemd services (state "failed" lists only failed ones).
func Services(ctx context.Context, state string) ([]Service, error) {
	args := []string{"list-units", "--type=service", "--all", "--no-pager", "--plain", "--no-legend", "--output=json"}
	if state != "" {
		if !regexp.MustCompile(`^[a-z-]{1,20}$`).MatchString(state) {
			return nil, errors.New("invalid state")
		}
		args = append(args, "--state="+state)
	}
	r := run(ctx, 30*time.Second, "systemctl", args...)
	var raw []struct {
		Unit, Load, Active, Sub, Description string
	}
	if err := json.Unmarshal([]byte(r.Stdout), &raw); err != nil {
		return nil, fmt.Errorf("systemctl: %s", strings.TrimSpace(r.Stderr+r.Stdout))
	}
	out := make([]Service, 0, len(raw))
	for _, s := range raw {
		out = append(out, Service{Name: s.Unit, Load: s.Load, Active: s.Active, Sub: s.Sub, Description: s.Description})
	}
	return out, nil
}

// ServiceStatus returns `systemctl status` plus the key properties of one unit.
func ServiceStatus(ctx context.Context, unit string) (map[string]any, error) {
	if !unitRE.MatchString(unit) {
		return nil, errors.New("invalid unit name")
	}
	props := run(ctx, 20*time.Second, "systemctl", "show", unit, "--no-pager",
		"-p", "Id,Description,LoadState,ActiveState,SubState,UnitFileState,MainPID,ExecMainStartTimestamp,NRestarts,Result,MemoryCurrent,FragmentPath")
	st := run(ctx, 20*time.Second, "systemctl", "status", unit, "--no-pager", "-l", "-n", "30")
	p := map[string]string{}
	sc := bufio.NewScanner(strings.NewReader(props.Stdout))
	for sc.Scan() {
		if k, v, ok := strings.Cut(sc.Text(), "="); ok {
			p[k] = v
		}
	}
	return map[string]any{"properties": p, "status": st.Stdout + st.Stderr}, nil
}

// ServiceAction starts, stops, restarts or reloads a unit.
func ServiceAction(ctx context.Context, unit, action string) (ExecResult, error) {
	if !unitRE.MatchString(unit) {
		return ExecResult{}, errors.New("invalid unit name")
	}
	switch action {
	case "start", "stop", "restart", "reload", "enable", "disable":
	default:
		return ExecResult{}, errors.New("action must be start, stop, restart, reload, enable or disable")
	}
	return run(ctx, 120*time.Second, "systemctl", action, unit), nil
}

type InstalledPackage struct {
	Name    string `json:"name"`
	Version string `json:"version"`
	Arch    string `json:"arch"`
	Status  string `json:"status"`
}

var pkgPattern = regexp.MustCompile(`^[a-z0-9.+*?-]{1,100}$`)

// Packages lists installed Debian packages matching a glob (e.g. "nginx*", "openssl").
func Packages(ctx context.Context, pattern string) ([]InstalledPackage, error) {
	if pattern == "" {
		pattern = "*"
	}
	if !pkgPattern.MatchString(pattern) {
		return nil, errors.New("invalid package pattern")
	}
	r := run(ctx, 60*time.Second, "dpkg-query", "-W", "-f", "${Package}\t${Version}\t${Architecture}\t${db:Status-Abbrev}\n", pattern)
	out := []InstalledPackage{}
	for _, line := range strings.Split(r.Stdout, "\n") {
		f := strings.Split(line, "\t")
		if len(f) < 4 || !strings.HasPrefix(f[3], "ii") {
			continue
		}
		out = append(out, InstalledPackage{Name: f[0], Version: f[1], Arch: f[2], Status: strings.TrimSpace(f[3])})
	}
	return out, nil
}

// ReadFile returns (the tail of) a text file, at most maxBytes (default 64 KiB, max 1 MiB).
func ReadFile(path string, maxBytes int, tail bool) (map[string]any, error) {
	if !strings.HasPrefix(path, "/") && runtime.GOOS != "windows" {
		return nil, errors.New("path must be absolute")
	}
	if maxBytes <= 0 {
		maxBytes = 64 * 1024
	}
	maxBytes = min(maxBytes, 1<<20)
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if st.IsDir() {
		entries, err := os.ReadDir(path)
		if err != nil {
			return nil, err
		}
		names := []string{}
		for i, e := range entries {
			if i >= 1000 {
				break
			}
			n := e.Name()
			if e.IsDir() {
				n += "/"
			}
			names = append(names, n)
		}
		return map[string]any{"path": path, "directory": true, "entries": names}, nil
	}
	size := st.Size()
	var start int64
	if tail && size > int64(maxBytes) {
		start = size - int64(maxBytes)
	}
	if _, err := f.Seek(start, io.SeekStart); err != nil {
		return nil, err
	}
	buf := make([]byte, maxBytes)
	n, _ := io.ReadFull(f, buf)
	return map[string]any{"path": path, "size": size, "offset": start, "content": string(buf[:n]),
		"truncated": int64(n) < size, "modified": st.ModTime().UTC().Format(time.RFC3339)}, nil
}
