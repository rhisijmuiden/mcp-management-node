//go:build !linux

package sysinfo

import "runtime"

// Windows and other systems: basic facts now, full metrics arrive with the Windows agent (phase 5).
func collectPlatform(m *Metrics) {}

func Info() map[string]any {
	return map[string]any{"os_pretty_name": runtime.GOOS}
}
