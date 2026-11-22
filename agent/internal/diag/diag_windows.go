//go:build windows

package diag

import (
	"context"
	"encoding/json"
	"strings"
	"time"

	"github.com/rhisijmuiden/mcp-management-node/agent/internal/winps"
)

func psJSON(ctx context.Context, script string, v any) error {
	out, _, _, err := winps.Output(ctx, 60*time.Second, script)
	if err != nil {
		return err
	}
	out = strings.TrimSpace(out)
	if strings.HasPrefix(out, "{") {
		out = "[" + out + "]"
	}
	if out == "" {
		out = "[]"
	}
	return json.Unmarshal([]byte(out), v)
}

func processes(ctx context.Context) ([]Proc, error) {
	var raw []struct {
		ID   int     `json:"Id"`
		Name string  `json:"ProcessName"`
		CPU  float64 `json:"CPU"`
		WS   uint64  `json:"WS"`
	}
	err := psJSON(ctx, `Get-Process | Select-Object Id,ProcessName,@{n='CPU';e={[math]::Round([double]$_.CPU,1)}},@{n='WS';e={[uint64]$_.WorkingSet64}} | ConvertTo-Json -Compress`, &raw)
	if err != nil {
		return nil, err
	}
	ps := make([]Proc, 0, len(raw))
	for _, p := range raw {
		ps = append(ps, Proc{PID: p.ID, Name: p.Name, CPU: p.CPU, MemByte: p.WS})
	}
	return ps, nil
}

func listening(ctx context.Context) ([]Listen, error) {
	var raw []struct {
		Proto   string `json:"p"`
		Address string `json:"a"`
		Process string `json:"n"`
	}
	err := psJSON(ctx, `$pn = @{}; Get-Process | ForEach-Object { $pn[$_.Id] = $_.ProcessName }
$t = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{ p='tcp'; a=('{0}:{1}' -f $_.LocalAddress, $_.LocalPort); n=$pn[[int]$_.OwningProcess] } }
$u = Get-NetUDPEndpoint -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -lt 49152 } | ForEach-Object { [pscustomobject]@{ p='udp'; a=('{0}:{1}' -f $_.LocalAddress, $_.LocalPort); n=$pn[[int]$_.OwningProcess] } }
@($t) + @($u) | Sort-Object p,a -Unique | ConvertTo-Json -Compress`, &raw)
	if err != nil {
		return nil, err
	}
	ls := make([]Listen, 0, len(raw))
	for _, l := range raw {
		ls = append(ls, Listen{Proto: l.Proto, Address: l.Address, Process: l.Process})
	}
	return ls, nil
}

func timeSync(ctx context.Context) string {
	out, _, code, err := winps.Output(ctx, 20*time.Second, `w32tm /query /status`)
	if err != nil || code != 0 {
		return "Windows Time service not running"
	}
	for _, l := range strings.Split(out, "\n") {
		if strings.HasPrefix(strings.TrimSpace(l), "Source:") {
			src := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(l), "Source:"))
			if strings.Contains(src, "Local CMOS Clock") || strings.Contains(src, "Free-running") {
				return "not synchronized (source: " + src + ")"
			}
			return "synchronized"
		}
	}
	return ""
}
