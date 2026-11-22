// Package ops gives the app (and through its MCP server, Claude) the everyday server-admin
// tools: logs, services, installed packages, files and commands. Every call is started by the
// app, which checks permissions and writes it to the audit log before it reaches the agent.
package ops

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/rhisijmuiden/stackpilot/agent/internal/winps"
)

const maxOutput = 256 * 1024

var unitRE = regexp.MustCompile(`^[A-Za-z0-9@._:\\-]{1,200}$`)

var safeArg = regexp.MustCompile(`^[A-Za-z0-9 :._+\-/]{0,64}$`)

var pkgPattern = regexp.MustCompile(`^[a-z0-9 .+*?_()-]{1,100}$`)

// GlobMatch matches a shell-style pattern (* and ?) against s.
func GlobMatch(pattern, s string) bool {
	ok, err := path.Match(pattern, s)
	return err == nil && ok
}

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

// Exec runs a shell command (as root; PowerShell as SYSTEM on Windows). Only reachable with the app's "exec" permission.
func Exec(ctx context.Context, command string, timeoutSec int) (ExecResult, error) {
	if strings.TrimSpace(command) == "" {
		return ExecResult{}, errors.New("empty command")
	}
	if runtime.GOOS == "windows" {
		return run(ctx, time.Duration(timeoutSec)*time.Second, winps.Exe(), winps.Args(command)...), nil
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
	// Windows only: the event log to read (System, Application, Security, ...). Default System+Application.
	Log string `json:"log"`
}

type Service struct {
	Name        string `json:"name"`
	Load        string `json:"load"`
	Active      string `json:"active"`
	Sub         string `json:"sub"`
	Description string `json:"description"`
}

type InstalledPackage struct {
	Name    string `json:"name"`
	Version string `json:"version"`
	Arch    string `json:"arch"`
	Status  string `json:"status"`
	Source  string `json:"source,omitempty"` // dpkg | rpm | apk | windows | hotfix
	Vendor  string `json:"vendor,omitempty"`
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

var relRE = regexp.MustCompile(`^(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hour|hours|d|day|days|w|week|weeks)\s+ago$`)

// ParseSince understands the journalctl-style times the app and Claude use: "1 hour ago",
// "30 min ago", "today", "yesterday", "2026-11-22", "2026-11-22 09:00[:00]" (local time) and RFC 3339.
func ParseSince(s string, now time.Time) (time.Time, error) {
	s = strings.ToLower(strings.TrimSpace(s))
	switch s {
	case "now":
		return now, nil
	case "today":
		y, m, d := now.Date()
		return time.Date(y, m, d, 0, 0, 0, 0, now.Location()), nil
	case "yesterday":
		y, m, d := now.AddDate(0, 0, -1).Date()
		return time.Date(y, m, d, 0, 0, 0, 0, now.Location()), nil
	}
	if m := relRE.FindStringSubmatch(s); m != nil {
		n, _ := strconv.Atoi(m[1])
		unit := time.Second
		switch m[2][0] {
		case 'm':
			unit = time.Minute
		case 'h':
			unit = time.Hour
		case 'd':
			unit = 24 * time.Hour
		case 'w':
			unit = 7 * 24 * time.Hour
		}
		return now.Add(-time.Duration(n) * unit), nil
	}
	for _, layout := range []string{time.RFC3339, "2006-01-02 15:04:05", "2006-01-02 15:04", "2006-01-02T15:04:05", "2006-01-02T15:04", "2006-01-02"} {
		if t, err := time.ParseInLocation(layout, strings.ToUpper(s), now.Location()); err == nil {
			return t, nil
		}
	}
	return time.Time{}, fmt.Errorf("cannot read the time %q (use e.g. '1 hour ago' or '2026-11-22 09:00')", s)
}
