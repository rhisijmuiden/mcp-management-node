// Package script runs a shell script sent by the app (cloud-image VMs, updates inside
// containers, template downloads, community scripts in default mode, scheduled custom commands)
// and streams its output line by line.
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
	"regexp"
	"runtime"
	"strings"
	"time"
)

var (
	envKeyRE = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]{0,63}$`)
	ansiRE   = regexp.MustCompile(`\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]|[\x00-\x08\x0b\x0c\x0e-\x1f]`)
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

// Run writes the script to a private temporary file and runs it with bash.
func Run(ctx context.Context, body string, env map[string]string, timeout time.Duration, line func(string)) (Result, error) {
	if runtime.GOOS == "windows" {
		return Result{}, errors.New("scripts are not supported on Windows yet")
	}
	if strings.TrimSpace(body) == "" {
		return Result{}, errors.New("empty script")
	}
	if timeout <= 0 || timeout > 6*time.Hour {
		timeout = time.Hour
	}
	f, err := os.CreateTemp("", "mmn-script-*.sh")
	if err != nil {
		return Result{}, err
	}
	defer os.Remove(f.Name())
	if _, err := f.WriteString(body); err != nil {
		f.Close()
		return Result{}, err
	}
	f.Close()
	if err := os.Chmod(f.Name(), 0o700); err != nil {
		return Result{}, err
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "/bin/bash", f.Name())
	cmd.Env = append(os.Environ(), "LC_ALL=C.UTF-8", "DEBIAN_FRONTEND=noninteractive", "TERM=dumb", "PAGER=cat")
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
