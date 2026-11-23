// Package winsvc runs the agent as a Windows service and installs or removes that service.
package winsvc

import "strings"

// RestartTask is the scheduled task that starts the agent service again if it is still stopped
// some time after a self-update. The service recovery actions normally restart the agent within
// seconds; this task is the safety net that does not depend on the agent process at all.
const RestartTask = "Stackpilot agent restart"

// RestartTaskScript is the PowerShell that registers RestartTask (as SYSTEM, once, delaySec
// seconds from now). The task checks the service every 15 seconds for 10 minutes and starts it
// when it is stopped. The Stackpilot app sends the same script to agents that still have the
// broken 0.7.1/0.8.0 restart (see backend mmn/services/nodes.py), so keep both in sync.
func RestartTaskScript(service string, delaySec int) string {
	loop := "for ($i = 0; $i -lt 40; $i++) { $s = Get-Service -Name '" + service + "' -ErrorAction SilentlyContinue; " +
		"if ($s -and $s.Status -eq 'Running') { break }; " +
		"if ($s -and $s.Status -eq 'Stopped') { Start-Service -Name '" + service + "' -ErrorAction SilentlyContinue }; " +
		"Start-Sleep -Seconds 15 }"
	return strings.Join([]string{
		"$ErrorActionPreference = 'Stop'",
		"$cmd = " + psQuote(loop),
		"$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))",
		"$ps = Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'",
		"$a = New-ScheduledTaskAction -Execute $ps -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ' + $enc)",
		"$t = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddSeconds(" + itoa(delaySec) + "))",
		"$st = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15)",
		"$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest",
		"Register-ScheduledTask -TaskName " + psQuote(RestartTask) + " -Action $a -Trigger $t -Settings $st -Principal $p -Force | Out-Null",
		"'armed'",
	}, "\n")
}

// RemoveTaskScript removes RestartTask once the agent runs again.
func RemoveTaskScript() string {
	return "Unregister-ScheduledTask -TaskName " + psQuote(RestartTask) + " -Confirm:$false -ErrorAction SilentlyContinue"
}

func psQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }

func itoa(n int) string {
	if n <= 0 {
		return "0"
	}
	var b []byte
	for ; n > 0; n /= 10 {
		b = append([]byte{byte('0' + n%10)}, b...)
	}
	return string(b)
}
