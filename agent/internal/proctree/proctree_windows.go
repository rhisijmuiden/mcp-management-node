//go:build windows

package proctree

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
)

// Cancelling runs taskkill /T, which stops the process and every process it started, and kills
// the process itself in case taskkill is missing or fails.
func setup(cmd *exec.Cmd) {
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		tk := "taskkill.exe"
		if root := os.Getenv("SystemRoot"); root != "" {
			tk = filepath.Join(root, "System32", "taskkill.exe")
		}
		_ = exec.Command(tk, "/T", "/F", "/PID", strconv.Itoa(cmd.Process.Pid)).Run()
		if err := cmd.Process.Kill(); err != nil && !errors.Is(err, os.ErrProcessDone) {
			return err
		}
		return nil
	}
}
