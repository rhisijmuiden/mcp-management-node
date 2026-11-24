//go:build windows

package netwatch

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/rhisijmuiden/stackpilot/agent/internal/winps"
)

const listScript = `$ErrorActionPreference = 'SilentlyContinue'
Get-NetAdapter -Physical | ForEach-Object {
  $w = if ($_.NdisPhysicalMedium -eq 9 -or $_.InterfaceDescription -match 'Wi-?Fi|Wireless|802\.11') { 1 } else { 0 }
  "NIC` + "`t" + `$($_.Name)` + "`t" + `$($_.InterfaceDescription)` + "`t" + `$($_.Status)` + "`t" + `$($_.LinkSpeed)` + "`t" + `$($_.MacAddress)` + "`t" + `$w"
}`

func state(s string) string {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "up":
		return "up"
	case "disconnected", "down", "lowerlayerdown":
		return "down"
	case "disabled":
		return "disabled"
	case "not present":
		return "absent"
	case "":
		return "unknown"
	}
	return strings.ToLower(strings.TrimSpace(s))
}

func parse(out string) []Adapter {
	var list []Adapter
	for _, line := range strings.Split(out, "\n") {
		p := strings.Split(strings.TrimRight(line, "\r"), "\t")
		if len(p) < 4 || p[0] != "NIC" || p[1] == "" {
			continue
		}
		for len(p) < 7 {
			p = append(p, "")
		}
		list = append(list, Adapter{Name: p[1], Description: p[2], Status: state(p[3]), Speed: p[4], MAC: p[5],
			Wireless: strings.TrimSpace(p[6]) == "1"})
	}
	return list
}

func listAdapters(ctx context.Context) ([]Adapter, error) {
	out, errOut, code, err := winps.Output(ctx, 60*time.Second, listScript)
	if err != nil {
		return nil, err
	}
	if code != 0 {
		return nil, fmt.Errorf("Get-NetAdapter: exit code %d: %s", code, strings.TrimSpace(errOut))
	}
	return parse(out), nil
}

func restartAdapter(ctx context.Context, name string) (string, error) {
	q := winps.Quote(name)
	script := "$ErrorActionPreference = 'Stop'\n" +
		"Restart-NetAdapter -Name " + q + " -Confirm:$false\n" +
		"for ($i = 0; $i -lt 20; $i++) { Start-Sleep -Seconds 3; if ((Get-NetAdapter -Name " + q + ").Status -eq 'Up') { break } }\n" +
		"(Get-NetAdapter -Name " + q + ").Status"
	out, errOut, code, err := winps.Output(ctx, 150*time.Second, script)
	if err != nil {
		return "unknown", err
	}
	if code != 0 {
		return "unknown", fmt.Errorf("Restart-NetAdapter: exit code %d: %s", code, strings.TrimSpace(errOut))
	}
	lines := strings.Split(strings.TrimSpace(out), "\n")
	return state(strings.TrimSpace(lines[len(lines)-1])), nil
}
