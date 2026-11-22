//go:build linux

package sysinfo

import (
	"bufio"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
)

func collectPlatform(m *Metrics) {
	m.CPUPercent = cpuPercent()
	if b, err := os.ReadFile(path("/proc/loadavg")); err == nil {
		f := strings.Fields(string(b))
		for i := 0; i < 3 && i < len(f); i++ {
			v, _ := strconv.ParseFloat(f[i], 64)
			m.Load = append(m.Load, v)
		}
	}
	mem := meminfo()
	m.MemTotal = mem["MemTotal"]
	if avail, ok := mem["MemAvailable"]; ok && m.MemTotal >= avail {
		m.MemUsed = m.MemTotal - avail
	}
	m.SwapTotal = mem["SwapTotal"]
	if m.SwapTotal >= mem["SwapFree"] {
		m.SwapUsed = m.SwapTotal - mem["SwapFree"]
	}
	m.DiskPath = "/"
	var st syscall.Statfs_t
	if err := syscall.Statfs(path("/"), &st); err == nil {
		m.DiskTotal = st.Blocks * uint64(st.Bsize)
		m.DiskUsed = (st.Blocks - st.Bfree) * uint64(st.Bsize)
	}
	if b, err := os.ReadFile(path("/proc/uptime")); err == nil {
		f := strings.Fields(string(b))
		if len(f) > 0 {
			v, _ := strconv.ParseFloat(f[0], 64)
			m.UptimeSec = uint64(v)
		}
	}
	if b, err := os.ReadFile(path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")); err == nil {
		m.Governor = strings.TrimSpace(string(b))
	}
	if b, err := os.ReadFile(path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")); err == nil {
		v, _ := strconv.ParseFloat(strings.TrimSpace(string(b)), 64)
		m.CPUFreqMHz = v / 1000
	}
}

// meminfo returns /proc/meminfo values in bytes.
func meminfo() map[string]uint64 {
	out := map[string]uint64{}
	f, err := os.Open(path("/proc/meminfo"))
	if err != nil {
		return out
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		k, rest, ok := strings.Cut(sc.Text(), ":")
		if !ok {
			continue
		}
		fields := strings.Fields(rest)
		if len(fields) == 0 {
			continue
		}
		v, _ := strconv.ParseUint(fields[0], 10, 64)
		if len(fields) > 1 && fields[1] == "kB" {
			v *= 1024
		}
		out[k] = v
	}
	return out
}

func readCPU() (idle, total uint64, ok bool) {
	b, err := os.ReadFile(path("/proc/stat"))
	if err != nil {
		return 0, 0, false
	}
	line, _, _ := strings.Cut(string(b), "\n")
	f := strings.Fields(line)
	if len(f) < 5 || f[0] != "cpu" {
		return 0, 0, false
	}
	for i, s := range f[1:] {
		v, _ := strconv.ParseUint(s, 10, 64)
		total += v
		if i == 3 || i == 4 { // idle + iowait
			idle += v
		}
	}
	return idle, total, true
}

// cpuPercent returns usage since the previous call (first call: since boot).
func cpuPercent() float64 {
	cpuMu.Lock()
	defer cpuMu.Unlock()
	idle, total, ok := readCPU()
	if !ok {
		return 0
	}
	pi, pt := lastIdle, lastTotal
	if !haveLastCPU {
		pi, pt = 0, 0
	}
	lastIdle, lastTotal, haveLastCPU = idle, total, true
	dt := float64(total - pt)
	if dt <= 0 {
		return 0
	}
	v := (1 - float64(idle-pi)/dt) * 100
	if v < 0 {
		v = 0
	}
	return float64(int(v*10+0.5)) / 10
}

// Info returns static facts about the host.
func Info() map[string]any {
	info := map[string]any{}
	if b, err := os.ReadFile(path("/etc/os-release")); err == nil {
		for _, line := range strings.Split(string(b), "\n") {
			if k, v, ok := strings.Cut(line, "="); ok && (k == "PRETTY_NAME" || k == "VERSION_CODENAME" || k == "ID") {
				info["os_"+strings.ToLower(k)] = strings.Trim(v, `"`)
			}
		}
	}
	var u syscall.Utsname
	if err := syscall.Uname(&u); err == nil {
		info["kernel"] = charsToString(u.Release[:])
	}
	if b, err := os.ReadFile(path("/proc/cpuinfo")); err == nil {
		for _, line := range strings.Split(string(b), "\n") {
			if k, v, ok := strings.Cut(line, ":"); ok && strings.TrimSpace(k) == "model name" {
				info["cpu_model"] = strings.TrimSpace(v)
				break
			}
		}
	}
	mem := meminfo()
	info["mem_total"] = mem["MemTotal"]
	if out, err := exec.Command("pveversion").Output(); err == nil {
		// "pve-manager/9.0.3/abcdef (running kernel: 6.14.8-2-pve)"
		s := strings.TrimSpace(string(out))
		info["pveversion"] = s
		if p := strings.Split(s, "/"); len(p) >= 2 {
			info["pve_version"] = p[1]
		}
	}
	if b, err := os.ReadFile(path("/sys/class/dmi/id/product_name")); err == nil {
		info["product"] = strings.TrimSpace(string(b))
	}
	if b, err := os.ReadFile(path("/sys/class/dmi/id/sys_vendor")); err == nil {
		info["vendor"] = strings.TrimSpace(string(b))
	}
	return info
}

func charsToString[T int8 | uint8](ca []T) string {
	b := make([]byte, 0, len(ca))
	for _, c := range ca {
		if c == 0 {
			break
		}
		b = append(b, byte(c))
	}
	return string(b)
}
