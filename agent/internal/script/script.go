// Package script runs a shell script sent by the app (cloud-image VMs, updates inside
// containers, template downloads, community scripts in default mode, scheduled custom commands)
// and streams its output line by line.
//
// On Linux the script runs with bash. On Windows (phase 15: scheduled tasks on Windows machines)
// it is a PowerShell script, written as a UTF-8 .ps1 file in the agent's private state folder and
// run with powershell.exe -File under the agent's account (LocalSystem).
package script

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/rhisijmuiden/stackpilot/agent/internal/config"
	"github.com/rhisijmuiden/stackpilot/agent/internal/winps"
)

var (
	protectOnce sync.Once
	protectErr  error
	envKeyRE    = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]{0,63}$`)
	ansiRE      = regexp.MustCompile(`\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]|[\x00-\x08\x0b\x0c\x0e-\x1f]`)
)

// Clean removes terminal colours and control characters from a line.
func Clean(s string) string {
	return strings.TrimRight(ansiRE.ReplaceAllString(s, ""), " \t")
}

type Result struct {
	ExitCode   int   `json:"exit_code"`
	DurationMs int64 `json:"duration_ms"`
	TimedOut   bool  `json:"timed_out,omitempty"`
}

// splitLines splits on \n and on \r (progress bars), so long downloads still show progress.
func splitLines(data []byte, atEOF bool) (int, []byte, error) {
	if atEOF && len(data) == 0 {
		return 0, nil, nil
	}
	if i := bytes.IndexAny(data, "\r\n"); i >= 0 {
		return i + 1, data[:i], nil
	}
	if atEOF || len(data) > 8192 {
		return len(data), data, nil
	}
	return 0, nil, nil
}

// Run writes the script to a private temporary file and runs it with bash (PowerShell on Windows).
func Run(ctx context.Context, body string, env map[string]string, timeout time.Duration, line func(string)) (Result, error) {
	if strings.TrimSpace(body) == "" {
		return Result{}, errors.New("empty script")
	}
	if timeout <= 0 || timeout > 6*time.Hour {
		timeout = time.Hour
	}
	windows := runtime.GOOS == "windows"
	dir, pattern, content := "", "mmn-script-*.sh", body
	if windows {
		// A folder that only SYSTEM and administrators can read or change.
		dir = filepath.Join(config.StateDir, "scripts")
		protectOnce.Do(func() { protectErr = config.Protect(dir) })
		if protectErr != nil {
			return Result{}, protectErr
		}
		// A BOM makes Windows PowerShell 5.1 read the file as UTF-8.
		pattern, content = "mmn-script-*.ps1", "\ufeff"+winps.Prelude+body
	}
	f, err := os.CreateTemp(dir, pattern)
	if err != nil {
		return Result{}, err
	}
	defer os.Remove(f.Name())
	if _, err := f.WriteString(content); err != nil {
		f.Close()
		return Result{}, err
	}
	f.Close()
	if err := os.Chmod(f.Name(), 0o700); err != nil && !windows {
		return Result{}, err
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var cmd *exec.Cmd
	if windows {
		cmd = exec.CommandContext(ctx, winps.Exe(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
			"-File", f.Name())
		cmd.Env = os.Environ()
	} else {
		cmd = exec.CommandContext(ctx, "/bin/bash", f.Name())
		cmd.Env = append(os.Environ(), "LC_ALL=C.UTF-8", "DEBIAN_FRONTEND=noninteractive", "TERM=dumb", "PAGER=cat")
	}
	for k, v := range env {
		if !envKeyRE.MatchString(k) {
			return Result{}, fmt.Errorf("invalid environment variable %q", k)
		}
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	cmd.Stdin = nil
	pr, pw := io.Pipe()
	cmd.Stdout, cmd.Stderr = pw, pw
	start := time.Now()
	if err := cmd.Start(); err != nil {
		return Result{}, err
	}
	done := make(chan struct{})
	go func() {
		sc := bufio.NewScanner(pr)
		sc.Buffer(make([]byte, 64*1024), 1<<20)
		sc.Split(splitLines)
		last := ""
		for sc.Scan() {
			l := Clean(sc.Text())
			if l == "" || l == last {
				continue
			}
			last = l
			line(l)
		}
		_, _ = io.Copy(io.Discard, pr)
		close(done)
	}()
	err = cmd.Wait()
	pw.Close()
	<-done
	r := Result{DurationMs: time.Since(start).Milliseconds()}
	var ee *exec.ExitError
	switch {
	case ctx.Err() == context.DeadlineExceeded:
		r.TimedOut, r.ExitCode = true, -1
		return r, errors.New("the script timed out")
	case errors.As(err, &ee):
		r.ExitCode = ee.ExitCode()
		return r, fmt.Errorf("exit code %d", r.ExitCode)
	case err != nil:
		r.ExitCode = -1
		return r, err
	}
	return r, nil
}
