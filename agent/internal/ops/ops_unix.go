//go:build !windows

package ops

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"time"
)

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

// Packages lists installed packages matching a glob (e.g. "nginx*", "openssl"): dpkg on Debian
// and Ubuntu, rpm on RHEL, Rocky, Alma, Fedora and SUSE.
func Packages(ctx context.Context, pattern string) ([]InstalledPackage, error) {
	if pattern == "" {
		pattern = "*"
	}
	pattern = strings.ToLower(pattern)
	if !pkgPattern.MatchString(pattern) {
		return nil, errors.New("invalid package pattern")
	}
	out := []InstalledPackage{}
	if _, err := exec.LookPath("dpkg-query"); err == nil {
		r := run(ctx, 60*time.Second, "dpkg-query", "-W", "-f", "${Package}\t${Version}\t${Architecture}\t${db:Status-Abbrev}\n", pattern)
		for _, line := range strings.Split(r.Stdout, "\n") {
			f := strings.Split(line, "\t")
			if len(f) < 4 || !strings.HasPrefix(f[3], "ii") {
				continue
			}
			out = append(out, InstalledPackage{Name: f[0], Version: f[1], Arch: f[2], Status: strings.TrimSpace(f[3]), Source: "dpkg"})
		}
		return out, nil
	}
	if _, err := exec.LookPath("rpm"); err == nil {
		r := run(ctx, 60*time.Second, "rpm", "-qa", "--qf", "%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n")
		for _, line := range strings.Split(r.Stdout, "\n") {
			f := strings.Split(line, "\t")
			if len(f) < 3 || !GlobMatch(pattern, strings.ToLower(f[0])) {
				continue
			}
			out = append(out, InstalledPackage{Name: f[0], Version: strings.TrimPrefix(f[1], "0:"), Arch: f[2], Status: "installed", Source: "rpm"})
		}
		return out, nil
	}
	if _, err := exec.LookPath("apk"); err == nil {
		r := run(ctx, 60*time.Second, "apk", "list", "--installed")
		for _, line := range strings.Split(r.Stdout, "\n") {
			// name-1.2.3-r0 x86_64 {origin} (license) [installed]
			f := strings.Fields(line)
			if len(f) < 2 {
				continue
			}
			name, ver := splitApk(f[0])
			if !GlobMatch(pattern, strings.ToLower(name)) {
				continue
			}
			out = append(out, InstalledPackage{Name: name, Version: ver, Arch: f[1], Status: "installed", Source: "apk"})
		}
		return out, nil
	}
	return nil, errors.New("no supported package manager found (dpkg, rpm or apk)")
}

// splitApk splits "openssl-3.3.2-r1" into ("openssl", "3.3.2-r1").
func splitApk(s string) (string, string) {
	parts := strings.Split(s, "-")
	for i := 1; i < len(parts); i++ {
		if len(parts[i]) > 0 && parts[i][0] >= '0' && parts[i][0] <= '9' {
			return strings.Join(parts[:i], "-"), strings.Join(parts[i:], "-")
		}
	}
	return s, ""
}

// FailedServiceCount counts failed systemd services.
func FailedServiceCount(ctx context.Context) (int, error) {
	if _, err := exec.LookPath("systemctl"); err != nil {
		return 0, err
	}
	r := run(ctx, 20*time.Second, "systemctl", "list-units", "--type=service", "--state=failed", "--no-legend", "--plain", "--no-pager")
	if r.ExitCode != 0 {
		return 0, errors.New(strings.TrimSpace(r.Stderr))
	}
	n := 0
	for _, l := range strings.Split(r.Stdout, "\n") {
		if strings.TrimSpace(l) != "" {
			n++
		}
	}
	return n, nil
}

// Events is only available on Windows; Linux uses the journal.
func Events(ctx context.Context, q JournalQuery) ([]Event, error) {
	return nil, errors.New("the Windows Event Log is only available on Windows servers; use the journal")
}
