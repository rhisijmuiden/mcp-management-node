//go:build windows

package ops

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"time"

	"golang.org/x/sys/windows/registry"

	"github.com/rhisijmuiden/stackpilot/agent/internal/config"
	"github.com/rhisijmuiden/stackpilot/agent/internal/restartlog"
	"github.com/rhisijmuiden/stackpilot/agent/internal/winps"
)

// Windows service names: letters, digits and a few separators (no quotes).
var winSvcRE = regexp.MustCompile(`^[A-Za-z0-9 _.$@{}#+-]{1,256}$`)

var logNameRE = regexp.MustCompile(`^[A-Za-z0-9 /._-]{1,128}$`)

func psRun(ctx context.Context, timeout time.Duration, script string) ExecResult {
	return run(ctx, timeout, winps.Exe(), winps.Args(script)...)
}

func psJSON(ctx context.Context, timeout time.Duration, script string, v any) error {
	r := psRun(ctx, timeout, script)
	out := strings.TrimSpace(r.Stdout)
	if out == "" {
		if r.ExitCode != 0 {
			return fmt.Errorf("powershell: %s", strings.TrimSpace(r.Stderr))
		}
		out = "[]"
	}
	// ConvertTo-Json returns an object (not an array) for a single item.
	if strings.HasPrefix(out, "{") {
		out = "[" + out + "]"
	}
	if err := json.Unmarshal([]byte(out), v); err != nil {
		return fmt.Errorf("powershell: unexpected output: %s", firstLine(out+" "+r.Stderr))
	}
	return nil
}

func firstLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	if len(s) > 300 {
		s = s[:300]
	}
	return s
}

type winService struct {
	Name        string `json:"Name"`
	DisplayName string `json:"DisplayName"`
	State       string `json:"State"`
	StartMode   string `json:"StartMode"`
	ExitCode    int    `json:"ExitCode"`
	Description string `json:"Description"`
}

// failed: an automatic service that stopped with an error (1077 = never started since boot).
func (s winService) failed() bool {
	return s.StartMode == "Auto" && s.State == "Stopped" && s.ExitCode != 0 && s.ExitCode != 1077
}

func winServices(ctx context.Context) ([]winService, error) {
	var raw []winService
	err := psJSON(ctx, 60*time.Second, `Get-CimInstance Win32_Service | Select-Object Name,DisplayName,State,StartMode,ExitCode,Description | ConvertTo-Json -Compress -Depth 2`, &raw)
	return raw, err
}

// Services lists Windows services. Active is active/inactive/failed like systemd, Sub is the
// Windows state (Running, Stopped, ...) and Load the start mode (Auto, Manual, Disabled).
func Services(ctx context.Context, state string) ([]Service, error) {
	raw, err := winServices(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]Service, 0, len(raw))
	for _, s := range raw {
		active := "inactive"
		switch {
		case s.State == "Running":
			active = "active"
		case s.failed():
			active = "failed"
		case s.State != "Stopped":
			active = "activating"
		}
		switch state {
		case "", "all":
		case "running":
			if s.State != "Running" {
				continue
			}
		case "failed":
			if active != "failed" {
				continue
			}
		case "stopped", "inactive":
			if s.State != "Stopped" {
				continue
			}
		}
		out = append(out, Service{Name: s.Name, Load: strings.ToLower(s.StartMode), Active: active, Sub: strings.ToLower(s.State),
			Description: s.DisplayName})
	}
	sort.Slice(out, func(i, j int) bool { return strings.ToLower(out[i].Name) < strings.ToLower(out[j].Name) })
	return out, nil
}

// FailedServiceCount counts automatic services that stopped with an error.
func FailedServiceCount(ctx context.Context) (int, error) {
	raw, err := winServices(ctx)
	if err != nil {
		return 0, err
	}
	n := 0
	for _, s := range raw {
		if s.failed() {
			n++
		}
	}
	return n, nil
}

// ServiceStatus returns the details of one service plus its recent Service Control Manager events.
func ServiceStatus(ctx context.Context, unit string) (map[string]any, error) {
	if !winSvcRE.MatchString(unit) {
		return nil, errors.New("invalid service name")
	}
	var raw []map[string]any
	q := winps.Quote(unit)
	err := psJSON(ctx, 60*time.Second, `$n = `+q+`
$s = Get-CimInstance Win32_Service | Where-Object { $_.Name -eq $n }
if (-not $s) { throw 'service not found' }
$s | Select-Object Name,DisplayName,State,Status,StartMode,DelayedAutoStart,ProcessId,ExitCode,ServiceSpecificExitCode,PathName,StartName,Description | ConvertTo-Json -Compress`, &raw)
	if err != nil {
		return nil, err
	}
	if len(raw) == 0 {
		return nil, errors.New("service not found")
	}
	p := map[string]string{}
	for k, v := range raw[0] {
		if v != nil {
			p[k] = fmt.Sprint(v)
		}
	}
	// Recent events about this service, newest first.
	ev := psRun(ctx, 60*time.Second, `$n = `+winps.Quote(p["DisplayName"])+`
Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Service Control Manager'} -MaxEvents 400 -ErrorAction SilentlyContinue |
  Where-Object { $_.Message -like ('*' + $n + '*') } | Select-Object -First 15 |
  ForEach-Object { '{0:yyyy-MM-dd HH:mm:ss} {1,-11} {2}' -f $_.TimeCreated, $_.LevelDisplayName, ($_.Message -replace '\s+', ' ') }`)
	var b strings.Builder
	fmt.Fprintf(&b, "%s (%s)\n  State:      %s\n  Start mode: %s", p["DisplayName"], p["Name"], p["State"], p["StartMode"])
	if p["DelayedAutoStart"] == "true" {
		b.WriteString(" (delayed)")
	}
	fmt.Fprintf(&b, "\n  PID:        %s\n  Exit code:  %s\n  Account:    %s\n  Path:       %s\n", p["ProcessId"], p["ExitCode"], p["StartName"], p["PathName"])
	if d := p["Description"]; d != "" {
		fmt.Fprintf(&b, "  %s\n", d)
	}
	if s := strings.TrimSpace(ev.Stdout); s != "" {
		b.WriteString("\nRecent events (Service Control Manager):\n" + s + "\n")
	}
	return map[string]any{"properties": p, "status": b.String()}, nil
}

// ServiceAction starts, stops, restarts, enables (automatic) or disables a Windows service.
func ServiceAction(ctx context.Context, unit, action string) (ExecResult, error) {
	if !winSvcRE.MatchString(unit) {
		return ExecResult{}, errors.New("invalid service name")
	}
	q := winps.Quote(unit)
	var script string
	switch action {
	case "start":
		script = "Start-Service -Name " + q
	case "stop":
		script = "Stop-Service -Name " + q + " -Force"
	case "restart":
		script = "Restart-Service -Name " + q + " -Force"
	case "enable":
		script = "Set-Service -Name " + q + " -StartupType Automatic"
	case "disable":
		script = "Set-Service -Name " + q + " -StartupType Disabled"
	default:
		return ExecResult{}, errors.New("action must be start, stop, restart, enable or disable")
	}
	r := psRun(ctx, 150*time.Second, "$ErrorActionPreference='Stop'\n"+script+"\n(Get-Service -Name "+q+" | Select-Object -ExpandProperty Status).ToString()")
	return r, nil
}

// Journal reads the Windows Event Log (by default System and Application) as text lines, the
// same shape the Linux agent returns for the systemd journal. Unit filters on the event source.
func Journal(ctx context.Context, q JournalQuery) (ExecResult, error) {
	script, err := eventScript(q, false)
	if err != nil {
		return ExecResult{}, err
	}
	return psRun(ctx, 120*time.Second, script), nil
}

// Events returns Event Log entries as structured data.
func Events(ctx context.Context, q JournalQuery) ([]Event, error) {
	script, err := eventScript(q, true)
	if err != nil {
		return nil, err
	}
	var out []Event
	if err := psJSON(ctx, 120*time.Second, script, &out); err != nil {
		return nil, err
	}
	markUpdateRestarts(out, restartlog.Load(config.StateDir))
	return out, nil
}

func eventScript(q JournalQuery, asJSON bool) (string, error) {
	if q.Lines <= 0 {
		q.Lines = 200
	}
	q.Lines = min(q.Lines, 5000)
	logs := []string{"System", "Application"}
	if q.Log != "" {
		logs = nil
		for _, l := range strings.Split(q.Log, ",") {
			l = strings.TrimSpace(l)
			if !logNameRE.MatchString(l) {
				return "", errors.New("invalid log name")
			}
			logs = append(logs, l)
		}
	}
	quoted := make([]string, len(logs))
	for i, l := range logs {
		quoted[i] = winps.Quote(l)
	}
	var b strings.Builder
	b.WriteString("$f = @{LogName=@(" + strings.Join(quoted, ",") + ")}\n")
	if lv := windowsLevels(q.Priority); lv != "" {
		b.WriteString("$f.Level = @(" + lv + ")\n")
	}
	if q.Unit != "" {
		if !winSvcRE.MatchString(q.Unit) {
			return "", errors.New("invalid event source")
		}
		b.WriteString("$f.ProviderName = " + winps.Quote(q.Unit) + "\n")
	}
	now := time.Now()
	if q.Since != "" {
		t, err := ParseSince(q.Since, now)
		if err != nil {
			return "", err
		}
		b.WriteString("$f.StartTime = [datetime]::Parse(" + winps.Quote(t.UTC().Format(time.RFC3339)) + ").ToLocalTime()\n")
	} else if q.Boot {
		b.WriteString("$f.StartTime = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime\n")
	}
	if q.Until != "" {
		t, err := ParseSince(q.Until, now)
		if err != nil {
			return "", err
		}
		b.WriteString("$f.EndTime = [datetime]::Parse(" + winps.Quote(t.UTC().Format(time.RFC3339)) + ").ToLocalTime()\n")
	}
	scan := q.Lines
	if q.Grep != "" {
		if len(q.Grep) > 200 {
			return "", errors.New("grep pattern too long")
		}
		if _, err := regexp.Compile(q.Grep); err != nil {
			return "", errors.New("invalid grep pattern")
		}
		scan = min(q.Lines*20, 50000)
	}
	fmt.Fprintf(&b, "$ev = @(Get-WinEvent -FilterHashtable $f -MaxEvents %d -ErrorAction SilentlyContinue)\n", scan)
	if q.Grep != "" {
		b.WriteString("$g = " + winps.Quote(q.Grep) + "\n$ev = @($ev | Where-Object { $_.Message -match $g -or $_.ProviderName -match $g })\n")
	}
	fmt.Fprintf(&b, "$ev = @($ev | Select-Object -First %d)\n[array]::Reverse($ev)\n", q.Lines)
	if asJSON {
		b.WriteString(`$ev | ForEach-Object { [pscustomobject]@{ time = $_.TimeCreated.ToUniversalTime().ToString('o'); log = $_.LogName; level = $_.LevelDisplayName; provider = $_.ProviderName; id = $_.Id; message = (($_.Message -replace '\s+', ' ').Trim()) } } | ConvertTo-Json -Compress -Depth 2`)
	} else {
		b.WriteString(`if ($ev.Count -eq 0) { '-- No entries --' }
$ev | ForEach-Object { '{0:yyyy-MM-ddTHH:mm:sszzz} {1} {2} {3}[{4}]: {5}' -f $_.TimeCreated, $_.LogName, $_.LevelDisplayName, $_.ProviderName, $_.Id, (($_.Message -replace '\s+', ' ').Trim()) }`)
	}
	return b.String(), nil
}

// windowsLevels maps a journald priority ("that level and worse") to Event Log levels:
// 1 Critical, 2 Error, 3 Warning, 4 Information, 5 Verbose.
func windowsLevels(p string) string {
	switch strings.ToLower(p) {
	case "emerg", "alert", "crit", "0", "1", "2", "critical":
		return "1"
	case "err", "error", "3":
		return "1,2"
	case "warning", "warn", "4":
		return "1,2,3"
	case "notice", "info", "5", "6", "information":
		return "1,2,3,4"
	}
	return ""
}

var kbRE = regexp.MustCompile(`^kb[0-9*?]*$`)

// Packages lists installed software (the "Apps & features" list) matching a glob on the name,
// and installed hotfixes when the pattern starts with "kb".
func Packages(ctx context.Context, pattern string) ([]InstalledPackage, error) {
	if pattern == "" {
		pattern = "*"
	}
	pattern = strings.ToLower(pattern)
	if !pkgPattern.MatchString(pattern) {
		return nil, errors.New("invalid package pattern")
	}
	if kbRE.MatchString(pattern) {
		var hf []struct {
			HotFixID    string `json:"HotFixID"`
			Description string `json:"Description"`
			InstalledOn string `json:"InstalledOn"`
		}
		if err := psJSON(ctx, 90*time.Second, `Get-HotFix | Select-Object HotFixID,Description,@{n='InstalledOn';e={if($_.InstalledOn){$_.InstalledOn.ToString('yyyy-MM-dd')}}} | ConvertTo-Json -Compress`, &hf); err != nil {
			return nil, err
		}
		out := []InstalledPackage{}
		for _, h := range hf {
			if GlobMatch(pattern, strings.ToLower(h.HotFixID)) {
				out = append(out, InstalledPackage{Name: h.HotFixID, Version: h.InstalledOn, Status: h.Description, Source: "hotfix", Vendor: "Microsoft"})
			}
		}
		return out, nil
	}
	seen := map[string]bool{}
	out := []InstalledPackage{}
	views := []struct {
		access uint32
		arch   string
	}{{registry.WOW64_64KEY, "x64"}, {registry.WOW64_32KEY, "x86"}}
	for _, v := range views {
		k, err := registry.OpenKey(registry.LOCAL_MACHINE, `SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall`,
			registry.ENUMERATE_SUB_KEYS|registry.QUERY_VALUE|v.access)
		if err != nil {
			continue
		}
		names, _ := k.ReadSubKeyNames(-1)
		for _, n := range names {
			sk, err := registry.OpenKey(k, n, registry.QUERY_VALUE|v.access)
			if err != nil {
				continue
			}
			name, _, _ := sk.GetStringValue("DisplayName")
			ver, _, _ := sk.GetStringValue("DisplayVersion")
			pub, _, _ := sk.GetStringValue("Publisher")
			sys, _, _ := sk.GetIntegerValue("SystemComponent")
			sk.Close()
			if name == "" || sys == 1 || !GlobMatch(pattern, strings.ToLower(name)) {
				continue
			}
			key := name + "\x00" + ver
			if seen[key] {
				continue
			}
			seen[key] = true
			out = append(out, InstalledPackage{Name: name, Version: ver, Arch: v.arch, Status: "installed", Source: "windows", Vendor: pub})
		}
		k.Close()
	}
	sort.Slice(out, func(i, j int) bool { return strings.ToLower(out[i].Name) < strings.ToLower(out[j].Name) })
	return out, nil
}
