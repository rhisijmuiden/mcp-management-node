//go:build !windows

package netwatch

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// SysClassNet is replaced in tests.
var SysClassNet = "/sys/class/net"

var ifaceRE = regexp.MustCompile(`^[A-Za-z0-9_.:@\-]{1,15}$`)

func read(p string) string {
	b, err := os.ReadFile(p)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// adapterOf reads one interface from /sys/class/net; ok is false for virtual interfaces.
func adapterOf(name string) (Adapter, bool) {
	d := filepath.Join(SysClassNet, name)
	if _, err := os.Stat(filepath.Join(d, "device")); err != nil {
		return Adapter{}, false
	}
	a := Adapter{Name: name, MAC: read(filepath.Join(d, "address"))}
	if link, err := os.Readlink(filepath.Join(d, "device", "driver")); err == nil {
		a.Description = filepath.Base(link)
	}
	if _, err := os.Stat(filepath.Join(d, "wireless")); err == nil {
		a.Wireless = true
	}
	flags, _ := strconv.ParseInt(strings.TrimPrefix(read(filepath.Join(d, "flags")), "0x"), 16, 64)
	switch {
	case flags&1 == 0:
		a.Status = "disabled"
	case read(filepath.Join(d, "carrier")) == "1":
		a.Status = "up"
	default:
		a.Status = "down"
	}
	if sp, err := strconv.Atoi(read(filepath.Join(d, "speed"))); err == nil && sp > 0 {
		if sp >= 1000 && sp%1000 == 0 {
			a.Speed = fmt.Sprintf("%d Gbps", sp/1000)
		} else {
			a.Speed = fmt.Sprintf("%d Mbps", sp)
		}
	}
	return a, true
}

func listAdapters(_ context.Context) ([]Adapter, error) {
	entries, err := os.ReadDir(SysClassNet)
	if err != nil {
		return nil, err
	}
	var out []Adapter
	for _, e := range entries {
		if a, ok := adapterOf(e.Name()); ok {
			out = append(out, a)
		}
	}
	return out, nil
}

func restartAdapter(ctx context.Context, name string) (string, error) {
	if !ifaceRE.MatchString(name) {
		return "unknown", fmt.Errorf("invalid interface name %q", name)
	}
	run := func(args ...string) error {
		c, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		out, err := exec.CommandContext(c, "ip", args...).CombinedOutput()
		if err != nil {
			return fmt.Errorf("ip %s: %v: %s", strings.Join(args, " "), err, strings.TrimSpace(string(out)))
		}
		return nil
	}
	if err := run("link", "set", "dev", name, "down"); err != nil {
		return "unknown", err
	}
	time.Sleep(2 * time.Second)
	if err := run("link", "set", "dev", name, "up"); err != nil {
		return "unknown", err
	}
	state := "down"
	for i := 0; i < 20; i++ {
		time.Sleep(3 * time.Second)
		if a, ok := adapterOf(name); ok {
			state = a.Status
			if state == "up" {
				break
			}
		}
	}
	return state, nil
}
