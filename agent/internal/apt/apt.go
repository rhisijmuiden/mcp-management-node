// Package apt checks for and installs package updates on Debian / Proxmox VE.
package apt

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/rhisijmuiden/stackpilot/agent/internal/proto"
)

var (
	// BootDir and RunningKernel can be replaced in tests.
	BootDir       = "/boot"
	RunningKernel = func() string {
		b, _ := os.ReadFile("/proc/sys/kernel/osrelease")
		return strings.TrimSpace(string(b))
	}
	mu sync.Mutex // one apt operation at a time
)

var ErrBusy = errors.New("another package operation is running")

func env() []string {
	return append(os.Environ(), "DEBIAN_FRONTEND=noninteractive", "APT_LISTCHANGES_FRONTEND=none", "LC_ALL=C.UTF-8",
		"NEEDRESTART_MODE=l")
}

// Available reports whether apt exists on this machine.
func Available() bool {
	_, err := exec.LookPath("apt-get")
	return err == nil
}

// ParseUpgradable parses the output of `apt list --upgradable`.
func ParseUpgradable(out string) []proto.Package {
	pkgs := []proto.Package{}
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "Listing") || strings.HasPrefix(line, "WARNING") || !strings.Contains(line, "/") {
			continue
		}
		// name/suite newver arch [upgradable from: oldver]
		f := strings.Fields(line)
		if len(f) < 3 {
			continue
		}
		name, suite, _ := strings.Cut(f[0], "/")
		p := proto.Package{Name: name, Suite: suite, New: f[1], Arch: f[2]}
		if i := strings.Index(line, "upgradable from: "); i >= 0 {
			p.Current = strings.TrimSuffix(strings.TrimSpace(line[i+len("upgradable from: "):]), "]")
		}
		pkgs = append(pkgs, p)
	}
	sort.Slice(pkgs, func(i, j int) bool { return pkgs[i].Name < pkgs[j].Name })
	return pkgs
}

// kernelKey makes "6.8.12-4-pve" comparable: numbers compare numerically.
func kernelKey(v string) []int {
	var out []int
	for _, part := range strings.FieldsFunc(v, func(r rune) bool { return r < '0' || r > '9' }) {
		n, _ := strconv.Atoi(part)
		out = append(out, n)
	}
	return out
}

func less(a, b []int) bool {
	for i := 0; i < len(a) && i < len(b); i++ {
		if a[i] != b[i] {
			return a[i] < b[i]
		}
	}
	return len(a) < len(b)
}

// RebootRequired is true when Debian flags it, or a newer kernel is installed than the one running.
func RebootRequired() bool {
	if _, err := os.Stat("/run/reboot-required"); err == nil {
		return true
	}
	running := RunningKernel()
	files, _ := filepath.Glob(filepath.Join(BootDir, "vmlinuz-*"))
	if running == "" || len(files) == 0 {
		return false
	}
	newest := ""
	for _, f := range files {
		v := strings.TrimPrefix(filepath.Base(f), "vmlinuz-")
		if newest == "" || less(kernelKey(newest), kernelKey(v)) {
			newest = v
		}
	}
	return newest != running && less(kernelKey(running), kernelKey(newest))
}

// Check runs `apt-get update` (when refresh) and lists upgradable packages.
func Check(ctx context.Context, refresh bool) proto.Updates {
	res := proto.Updates{CheckedAt: time.Now().UTC().Format(time.RFC3339), Packages: []proto.Package{}, RunningKernel: RunningKernel()}
	if !Available() {
		res.Error = "apt is not available on this machine"
		return res
	}
	if !mu.TryLock() {
		res.Error = ErrBusy.Error()
		return res
	}
	defer mu.Unlock()
	if refresh {
		cmd := exec.CommandContext(ctx, "apt-get", "update", "-q")
		cmd.Env = env()
		if out, err := cmd.CombinedOutput(); err != nil {
			res.Error = "apt-get update: " + lastLines(string(out), 6)
		}
	}
	cmd := exec.CommandContext(ctx, "apt", "list", "--upgradable")
	cmd.Env = env()
	out, err := cmd.Output()
	if err != nil && res.Error == "" {
		res.Error = "apt list: " + err.Error()
	}
	res.Packages = ParseUpgradable(string(out))
	res.RebootRequired = RebootRequired()
	return res
}

// Apply runs a full upgrade (the way Proxmox VE should be upgraded) and streams its output.
func Apply(ctx context.Context, line func(string)) error {
	if !Available() {
		return errors.New("apt is not available on this machine")
	}
	if !mu.TryLock() {
		return ErrBusy
	}
	defer mu.Unlock()
	steps := [][]string{
		{"apt-get", "update", "-q"},
		{"apt-get", "-y", "-q", "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold",
			"dist-upgrade"},
		{"apt-get", "-y", "-q", "autoremove"},
	}
	for _, s := range steps {
		line("$ " + strings.Join(s, " "))
		if err := stream(ctx, s, line); err != nil {
			return fmt.Errorf("%s failed: %w", strings.Join(s[:2], " "), err)
		}
	}
	return nil
}

func stream(ctx context.Context, argv []string, line func(string)) error {
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Env = env()
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
			line(sc.Text())
		}
		close(done)
	}()
	err := cmd.Wait()
	pw.Close()
	<-done
	return err
}

func lastLines(s string, n int) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, " | ")
}
