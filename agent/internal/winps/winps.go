// Package winps runs PowerShell scripts for the Windows agent and quotes values safely.
package winps

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"
	"os/exec"
	"strings"
	"time"
	"unicode/utf16"
)

// Prelude makes PowerShell print UTF-8 and stop on errors unless a command says otherwise.
const Prelude = "$ProgressPreference='SilentlyContinue';[Console]::OutputEncoding=[Text.Encoding]::UTF8;$OutputEncoding=[Text.Encoding]::UTF8;\n"

// Quote returns s as a PowerShell single-quoted string literal.
func Quote(s string) string {
	s = strings.NewReplacer("'", "''", "\u2018", "\u2018\u2018", "\u2019", "\u2019\u2019", "\u201a", "\u201a\u201a", "\u201b", "\u201b\u201b").Replace(s)
	return "'" + s + "'"
}

// Encode turns a script into the argument of -EncodedCommand (base64 of UTF-16LE).
func Encode(script string) string {
	u := utf16.Encode([]rune(script))
	var b bytes.Buffer
	for _, c := range u {
		_ = binary.Write(&b, binary.LittleEndian, c)
	}
	return base64.StdEncoding.EncodeToString(b.Bytes())
}

// Args are the powershell.exe arguments that run script without a profile or prompts.
func Args(script string) []string {
	return []string{"-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", Encode(Prelude + script)}
}

// Exe is the Windows PowerShell executable (always present on Windows Server 2016+ and Windows 10+).
func Exe() string {
	if p, err := exec.LookPath("powershell.exe"); err == nil {
		return p
	}
	return `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`
}

// Output runs a script and returns stdout, stderr and the exit code.
func Output(ctx context.Context, timeout time.Duration, script string) (string, string, int, error) {
	if timeout <= 0 {
		timeout = 60 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, Exe(), Args(script)...)
	var out, errb bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errb
	err := cmd.Run()
	code := 0
	if ee, ok := err.(*exec.ExitError); ok {
		code = ee.ExitCode()
		err = nil
	}
	return out.String(), errb.String(), code, err
}
