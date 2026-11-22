// Package governor reads and sets the CPU frequency governor (Linux cpufreq).
//
// The desired governor is stored in the agent state and applied again at every agent start,
// so the choice survives reboots without extra packages (cpufrequtils, ...).
package governor

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"sort"
	"strings"

	"github.com/rhisijmuiden/mcp-management-node/agent/internal/proto"
)

// SysRoot lets tests use a fake sysfs.
var SysRoot = "/sys/devices/system/cpu"

var ErrUnsupported = errors.New("CPU frequency scaling is not available on this machine")

func cpuDirs() []string {
	dirs, _ := filepath.Glob(filepath.Join(SysRoot, "cpu[0-9]*", "cpufreq"))
	sort.Strings(dirs)
	return dirs
}

func read(p string) string {
	b, err := os.ReadFile(p)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// Get returns the current governor (of cpu0; "mixed" if CPUs differ) and the available ones.
func Get() (*proto.GovernorInfo, error) {
	if runtime.GOOS != "linux" {
		return nil, ErrUnsupported
	}
	dirs := cpuDirs()
	if len(dirs) == 0 {
		return nil, ErrUnsupported
	}
	info := &proto.GovernorInfo{
		Available: strings.Fields(read(filepath.Join(dirs[0], "scaling_available_governors"))),
		Driver:    read(filepath.Join(dirs[0], "scaling_driver")),
	}
	cur := ""
	for _, d := range dirs {
		g := read(filepath.Join(d, "scaling_governor"))
		if cur == "" {
			cur = g
		} else if g != cur {
			cur = "mixed"
			break
		}
	}
	info.Current = cur
	return info, nil
}

// Set writes the governor for every CPU.
func Set(g string) (*proto.GovernorInfo, error) {
	info, err := Get()
	if err != nil {
		return nil, err
	}
	if len(info.Available) > 0 && !slices.Contains(info.Available, g) {
		return nil, fmt.Errorf("governor %q is not available here (available: %s)", g, strings.Join(info.Available, ", "))
	}
	var failed []string
	for _, d := range cpuDirs() {
		if err := os.WriteFile(filepath.Join(d, "scaling_governor"), []byte(g), 0o644); err != nil {
			failed = append(failed, filepath.Base(filepath.Dir(d))+": "+err.Error())
		}
	}
	if len(failed) > 0 {
		return nil, fmt.Errorf("could not set the governor: %s", strings.Join(failed, "; "))
	}
	return Get()
}
