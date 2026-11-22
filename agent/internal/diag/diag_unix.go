//go:build !windows

package diag

import (
	"context"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"time"
)

func output(ctx context.Context, name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	b, err := exec.CommandContext(ctx, name, args...).Output()
	return string(b), err
}

func processes(ctx context.Context) ([]Proc, error) {
	out, err := output(ctx, "ps", "-eo", "pid=,user=,pcpu=,rss=,comm=")
	if err != nil {
		return nil, err
	}
	var ps []Proc
	for _, l := range strings.Split(out, "\n") {
		f := strings.Fields(l)
		if len(f) < 5 {
			continue
		}
		pid, _ := strconv.Atoi(f[0])
		cpu, _ := strconv.ParseFloat(f[2], 64)
		rss, _ := strconv.ParseUint(f[3], 10, 64)
		ps = append(ps, Proc{PID: pid, User: f[1], CPU: cpu, MemByte: rss * 1024, Name: strings.Join(f[4:], " ")})
	}
	return ps, nil
}

var ssProc = regexp.MustCompile(`users:\(\("([^"]+)"`)

func listening(ctx context.Context) ([]Listen, error) {
	out, err := output(ctx, "ss", "-tulpnH")
	if err != nil {
		return nil, err
	}
	seen := map[string]bool{}
	var ls []Listen
	for _, l := range strings.Split(out, "\n") {
		f := strings.Fields(l)
		if len(f) < 5 {
			continue
		}
		p := ""
		if m := ssProc.FindStringSubmatch(l); m != nil {
			p = m[1]
		}
		k := f[0] + f[4]
		if seen[k] {
			continue
		}
		seen[k] = true
		ls = append(ls, Listen{Proto: f[0], Address: f[4], Process: p})
	}
	return ls, nil
}

func timeSync(ctx context.Context) string {
	out, err := output(ctx, "timedatectl", "show", "-p", "NTPSynchronized", "--value")
	if err != nil {
		return ""
	}
	if strings.TrimSpace(out) == "yes" {
		return "synchronized"
	}
	return "not synchronized with NTP"
}
