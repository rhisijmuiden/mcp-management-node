// Package diag collects a quick health check of a server in one call: resources, full disks,
// failed services, recent errors, busiest processes, listening ports and time sync. It is what
// Claude asks first when something "doesn't work" on a server.
package diag

import (
	"context"
	"fmt"
	"sort"
	"sync"
	"time"

	"github.com/rhisijmuiden/stackpilot/agent/internal/ops"
	"github.com/rhisijmuiden/stackpilot/agent/internal/sysinfo"
)

type Report struct {
	At             string          `json:"at"`
	Hostname       string          `json:"hostname"`
	Metrics        sysinfo.Metrics `json:"metrics"`
	Findings       []string        `json:"findings"` // short, human readable
	FailedServices []ops.Service   `json:"failed_services"`
	RecentErrors   string          `json:"recent_errors"`
	TopCPU         []Proc          `json:"top_cpu"`
	TopMemory      []Proc          `json:"top_memory"`
	Listening      []Listen        `json:"listening"`
	TimeSync       string          `json:"time_sync,omitempty"`
	Errors         []string        `json:"errors,omitempty"`
}

type Proc struct {
	PID     int     `json:"pid"`
	Name    string  `json:"name"`
	User    string  `json:"user,omitempty"`
	CPU     float64 `json:"cpu"` // percent (Linux) or CPU seconds (Windows)
	MemByte uint64  `json:"mem_bytes"`
}

type Listen struct {
	Proto   string `json:"proto"`
	Address string `json:"address"`
	Process string `json:"process,omitempty"`
}

// Since is how far back recent errors are read.
var Since = "1 hour ago"

func Run(ctx context.Context) *Report {
	r := &Report{At: time.Now().UTC().Format(time.RFC3339), Hostname: sysinfo.Hostname()}
	r.Metrics = sysinfo.Collect()
	var wg sync.WaitGroup
	var mu sync.Mutex
	fail := func(what string, err error) {
		mu.Lock()
		r.Errors = append(r.Errors, what+": "+err.Error())
		mu.Unlock()
	}
	wg.Add(5)
	go func() {
		defer wg.Done()
		s, err := ops.Services(ctx, "failed")
		if err != nil {
			fail("services", err)
			return
		}
		r.FailedServices = s
	}()
	go func() {
		defer wg.Done()
		e, err := ops.Journal(ctx, ops.JournalQuery{Since: Since, Priority: "err", Lines: 40})
		if err != nil {
			fail("logs", err)
			return
		}
		r.RecentErrors = e.Stdout
	}()
	go func() {
		defer wg.Done()
		p, err := processes(ctx)
		if err != nil {
			fail("processes", err)
			return
		}
		cpu := append([]Proc(nil), p...)
		sort.Slice(cpu, func(i, j int) bool { return cpu[i].CPU > cpu[j].CPU })
		mem := append([]Proc(nil), p...)
		sort.Slice(mem, func(i, j int) bool { return mem[i].MemByte > mem[j].MemByte })
		r.TopCPU, r.TopMemory = cpu[:min(8, len(cpu))], mem[:min(8, len(mem))]
	}()
	go func() {
		defer wg.Done()
		l, err := listening(ctx)
		if err != nil {
			fail("ports", err)
			return
		}
		r.Listening = l
	}()
	go func() {
		defer wg.Done()
		r.TimeSync = timeSync(ctx)
	}()
	wg.Wait()
	// The second CPU sample makes cpu_percent meaningful.
	m := sysinfo.Collect()
	r.Metrics.CPUPercent = m.CPUPercent
	r.Findings = findings(r)
	return r
}

func pct(used, total uint64) float64 {
	if total == 0 {
		return 0
	}
	return float64(used) / float64(total) * 100
}

func findings(r *Report) []string {
	out := []string{}
	m := r.Metrics
	for _, d := range m.Disks {
		if p := pct(d.Used, d.Total); p >= 85 {
			out = append(out, fmt.Sprintf("disk %s is %.0f%% full (%.1f GiB free)", d.Path, p, float64(d.Total-d.Used)/(1<<30)))
		}
	}
	if p := pct(m.MemUsed, m.MemTotal); p >= 90 {
		out = append(out, fmt.Sprintf("memory is %.0f%% used", p))
	}
	if m.SwapTotal > 0 && pct(m.SwapUsed, m.SwapTotal) >= 50 {
		out = append(out, fmt.Sprintf("swap/page file is %.0f%% used", pct(m.SwapUsed, m.SwapTotal)))
	}
	if len(m.Load) > 0 && m.CPUs > 0 && m.Load[0] > float64(m.CPUs)*1.5 {
		out = append(out, fmt.Sprintf("load %.2f on %d CPUs", m.Load[0], m.CPUs))
	}
	if m.CPUPercent >= 90 {
		out = append(out, fmt.Sprintf("CPU at %.0f%%", m.CPUPercent))
	}
	for _, s := range r.FailedServices {
		out = append(out, "service failed: "+s.Name)
	}
	if r.TimeSync != "" && r.TimeSync != "synchronized" {
		out = append(out, "clock: "+r.TimeSync)
	}
	return out
}
