// Package sysinfo collects host information and live metrics.
package sysinfo

import (
	"os"
	"runtime"
	"sync"
	"time"
)

type Metrics struct {
	At         string    `json:"at"`
	CPUPercent float64   `json:"cpu_percent"`
	CPUs       int       `json:"cpus"`
	Load       []float64 `json:"load,omitempty"`
	MemTotal   uint64    `json:"mem_total"`
	MemUsed    uint64    `json:"mem_used"`
	SwapTotal  uint64    `json:"swap_total"`
	SwapUsed   uint64    `json:"swap_used"`
	DiskTotal  uint64    `json:"disk_total"`
	DiskUsed   uint64    `json:"disk_used"`
	DiskPath   string    `json:"disk_path"`
	UptimeSec  uint64    `json:"uptime_seconds"`
	Guests     *Guests   `json:"guests,omitempty"`
	Governor   string    `json:"governor,omitempty"`
	CPUFreqMHz float64   `json:"cpu_mhz,omitempty"`
	// Disks lists every local file system (Linux) or fixed drive (Windows).
	Disks []Disk `json:"disks,omitempty"`
	// FailedServices is the number of failed services (systemd) or automatic Windows services
	// that stopped with an error; nil when unknown.
	FailedServices *int `json:"failed_services,omitempty"`
	Processes      int  `json:"processes,omitempty"`
}

type Disk struct {
	Path  string `json:"path"`
	FS    string `json:"fs,omitempty"`
	Total uint64 `json:"total"`
	Used  uint64 `json:"used"`
}

type Guests struct {
	VMsRunning int `json:"vms_running"`
	VMsTotal   int `json:"vms_total"`
	CTsRunning int `json:"cts_running"`
	CTsTotal   int `json:"cts_total"`
}

// Root lets tests point /proc and /sys elsewhere.
var Root = ""

func path(p string) string { return Root + p }

var (
	cpuMu       sync.Mutex
	lastIdle    uint64
	lastTotal   uint64
	haveLastCPU bool
)

// Hostname returns the short host name.
func Hostname() string {
	h, _ := os.Hostname()
	return h
}

// Collect gathers metrics. Guests are added by the caller (Proxmox specific).
func Collect() Metrics {
	m := Metrics{At: time.Now().UTC().Format(time.RFC3339), CPUs: runtime.NumCPU()}
	collectPlatform(&m)
	return m
}
