// Package winsvc runs the agent as a Windows service and installs or removes that service.
package winsvc

import "fmt"

// RestartScript is the PowerShell that a detached helper runs after a self-update: it waits
// for the service to stop (the agent stops it cleanly) and then starts it again, so the new
// version runs without Windows treating the update as a crash. If the service is not stopped
// after two minutes the helper gives up; the agent then falls back to exiting with an error,
// which makes the service recovery restart it.
func RestartScript(name string) string {
	return fmt.Sprintf("$ErrorActionPreference='SilentlyContinue'; $n='%s'; "+
		"for ($i = 0; $i -lt 60; $i++) { Start-Sleep -Seconds 2; "+
		"$s = Get-Service -Name $n; if ($s -and $s.Status -eq 'Stopped') { Start-Service -Name $n; break } }", name)
}
