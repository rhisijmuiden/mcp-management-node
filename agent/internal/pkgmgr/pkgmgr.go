// Package pkgmgr checks for and installs updates with the package manager of the machine:
// apt (Debian, Ubuntu, Proxmox VE), dnf/yum (RHEL, Rocky, Alma, Fedora) or Windows Update.
package pkgmgr

import (
	"bufio"
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/rhisijmuiden/mcp-management-node/agent/internal/apt"
	"github.com/rhisijmuiden/mcp-management-node/agent/internal/proto"
)

var mu sync.Mutex // one dnf / Windows Update operation at a time (apt has its own lock)

// Name returns the update system of this machine: apt, dnf, yum, windows-update or "".
func Name() string {
	if isWindows {
		return "windows-update"
	}
	if apt.Available() {
		return "apt"
	}
	for _, n := range []string{"dnf", "yum"} {
		if _, err := exec.LookPath(n); err == nil {
			return n
		}
	}
	return ""
}

// Available reports whether updates can be managed here.
func Available() bool { return Name() != "" }

// Check lists pending updates (refresh fetches the package lists first where that applies).
func Check(ctx context.Context, refresh bool) proto.Updates {
	switch n := Name(); n {
	case "apt":
		return apt.Check(ctx, refresh)
	case "dnf", "yum":
		return dnfCheck(ctx, n, refresh)
	case "windows-update":
		return winCheck(ctx)
	}
	return proto.Updates{CheckedAt: now(), Packages: []proto.Package{}, Error: "no supported package manager on this machine"}
}

// Apply installs all pending updates and streams the output.
func Apply(ctx context.Context, line func(string)) error {
	switch n := Name(); n {
	case "apt":
		return apt.Apply(ctx, line)
	case "dnf", "yum":
		if !mu.TryLock() {
			return apt.ErrBusy
		}
		defer mu.Unlock()
		argv := []string{n, "-y", "upgrade"}
		line("$ " + strings.Join(argv, " "))
		return Stream(ctx, argv, nil, line)
	case "windows-update":
		if !mu.TryLock() {
			return apt.ErrBusy
		}
		defer mu.Unlock()
		return winApply(ctx, line)
	}
	return errors.New("no supported package manager on this machine")
}

func now() string { return time.Now().UTC().Format(time.RFC3339) }

// ParseDnf parses `dnf check-update` output: "name.arch  version  repo" lines.
func ParseDnf(out string, installed map[string]string) []proto.Package {
	pkgs := []proto.Package{}
	for _, line := range strings.Split(out, "\n") {
		if strings.HasPrefix(line, "Obsoleting") || strings.HasPrefix(line, "Security:") {
			break
		}
		f := strings.Fields(line)
		if len(f) != 3 || strings.HasPrefix(line, " ") || !strings.Contains(f[0], ".") {
			continue
		}
		i := strings.LastIndexByte(f[0], '.')
		name, arch := f[0][:i], f[0][i+1:]
		pkgs = append(pkgs, proto.Package{Name: name, Arch: arch, New: f[1], Suite: f[2], Current: installed[name]})
	}
	sort.Slice(pkgs, func(i, j int) bool { return pkgs[i].Name < pkgs[j].Name })
	return pkgs
}

func dnfCheck(ctx context.Context, bin string, refresh bool) proto.Updates {
	res := proto.Updates{CheckedAt: now(), Packages: []proto.Package{}}
	if !mu.TryLock() {
		res.Error = apt.ErrBusy.Error()
		return res
	}
	defer mu.Unlock()
	args := []string{"-q", "check-update"}
	if refresh {
		args = append(args, "--refresh")
	}
	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Env = append(os.Environ(), "LC_ALL=C.UTF-8")
	out, err := cmd.Output()
	var ee *exec.ExitError
	if err != nil && !(errors.As(err, &ee) && ee.ExitCode() == 100) {
		res.Error = bin + " check-update: " + err.Error()
	}
	installed := map[string]string{}
	if b, err := exec.CommandContext(ctx, "rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\n").Output(); err == nil {
		for _, l := range strings.Split(string(b), "\n") {
			if n, v, ok := strings.Cut(l, "\t"); ok {
				installed[n] = v
			}
		}
	}
	res.Packages = ParseDnf(string(out), installed)
	res.RebootRequired = apt.RebootRequired()
	if c := exec.CommandContext(ctx, "needs-restarting", "-r"); c != nil {
		if err := c.Run(); err != nil && errors.As(err, &ee) && ee.ExitCode() == 1 {
			res.RebootRequired = true
		}
	}
	return res
}

// Stream runs a command and passes every output line to line.
func Stream(ctx context.Context, argv []string, env []string, line func(string)) error {
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Env = append(os.Environ(), env...)
	pr, pw := io.Pipe()
	cmd.Stdout, cmd.Stderr = pw, pw
	if err := cmd.Start(); err != nil {
		return err
	}
	done := make(chan struct{})
	go func() {
		sc := bufio.NewScanner(pr)
		sc.Buffer(make([]byte, 64*1024), 1024*1024)
		for sc.Scan() {
			line(strings.TrimRight(sc.Text(), "\r"))
		}
		close(done)
	}()
	err := cmd.Wait()
	pw.Close()
	<-done
	return err
}
