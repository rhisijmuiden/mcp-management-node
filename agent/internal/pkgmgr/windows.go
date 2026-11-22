//go:build windows

package pkgmgr

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"github.com/rhisijmuiden/mcp-management-node/agent/internal/proto"
	"github.com/rhisijmuiden/mcp-management-node/agent/internal/winps"
)

const isWindows = true

// The Windows Update Agent API (COM), the same one Settings > Windows Update and PSWindowsUpdate use.
const wuSearch = `$s = New-Object -ComObject Microsoft.Update.Session
$s.ClientApplicationID = 'Stackpilot'
$r = $s.CreateUpdateSearcher().Search("IsInstalled=0 and IsHidden=0 and Type='Software'")
`

const wuCheck = wuSearch + `$list = foreach ($u in $r.Updates) {
  [pscustomobject]@{ title = $u.Title; kb = ((@($u.KBArticleIDs) | ForEach-Object { 'KB' + $_ }) -join ','); severity = [string]$u.MsrcSeverity
    size = [int64]$u.MaxDownloadSize; categories = ((@($u.Categories) | ForEach-Object { $_.Name }) -join ', ') }
}
$reboot = (New-Object -ComObject Microsoft.Update.SystemInfo).RebootRequired
[pscustomobject]@{ updates = @($list); reboot = [bool]$reboot } | ConvertTo-Json -Compress -Depth 4`

const wuApply = wuSearch + `if ($r.Updates.Count -eq 0) { 'No updates to install.'; exit 0 }
$c = New-Object -ComObject Microsoft.Update.UpdateColl
foreach ($u in $r.Updates) { if (-not $u.EulaAccepted) { $u.AcceptEula() }; [void]$c.Add($u); 'queued: ' + $u.Title }
'downloading ' + $c.Count + ' update(s)...'
$d = $s.CreateUpdateDownloader(); $d.Updates = $c; $dr = $d.Download()
$names = @('not started', 'in progress', 'succeeded', 'succeeded with errors', 'failed', 'aborted')
'download: ' + $names[$dr.ResultCode]
'installing...'
$i = $s.CreateUpdateInstaller(); $i.Updates = $c; $ir = $i.Install()
for ($k = 0; $k -lt $c.Count; $k++) { '{0}: {1}' -f $c.Item($k).Title, $names[$ir.GetUpdateResult($k).ResultCode] }
'result: ' + $names[$ir.ResultCode] + '; reboot required: ' + $ir.RebootRequired
if ($ir.ResultCode -ge 4) { exit 1 }`

func winCheck(ctx context.Context) proto.Updates {
	res := proto.Updates{CheckedAt: now(), Packages: []proto.Package{}}
	if !mu.TryLock() {
		res.Error = "another update operation is running"
		return res
	}
	defer mu.Unlock()
	out, stderr, code, err := winps.Output(ctx, 20*time.Minute, wuCheck)
	if err != nil || code != 0 {
		msg := strings.TrimSpace(stderr)
		if err != nil {
			msg = err.Error()
		}
		res.Error = "Windows Update: " + firstLine(msg)
		return res
	}
	var r struct {
		Updates []struct {
			Title      string `json:"title"`
			KB         string `json:"kb"`
			Severity   string `json:"severity"`
			Size       int64  `json:"size"`
			Categories string `json:"categories"`
		} `json:"updates"`
		Reboot bool `json:"reboot"`
	}
	if err := json.Unmarshal([]byte(strings.TrimSpace(out)), &r); err != nil {
		res.Error = "Windows Update: unexpected answer"
		return res
	}
	for _, u := range r.Updates {
		name := u.KB
		if name == "" {
			name = u.Title
		}
		suite := u.Categories
		if u.Severity != "" {
			suite = strings.TrimSpace(u.Severity + " · " + suite)
		}
		res.Packages = append(res.Packages, proto.Package{Name: name, New: u.Title, Suite: suite})
	}
	res.RebootRequired = r.Reboot
	return res
}

func winApply(ctx context.Context, line func(string)) error {
	line("Windows Update: searching for updates")
	err := Stream(ctx, append([]string{winps.Exe()}, winps.Args(wuApply)...), nil, line)
	if err != nil {
		return errors.New("Windows Update did not finish successfully: " + err.Error())
	}
	return nil
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	return strings.TrimSpace(s)
}
