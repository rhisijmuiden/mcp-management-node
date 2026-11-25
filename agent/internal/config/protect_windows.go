//go:build windows

package config

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// Protect limits a folder to SYSTEM and the Administrators group (no inherited rights from
// %ProgramData%, where every user can read), for the keys, the state and the scripts that run.
// The files in it inherit those rights (see protectCommands).
func Protect(dir string) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	cmds := protectCommands(dir)
	if out, err := exec.Command("icacls", cmds[0]...).CombinedOutput(); err != nil {
		return fmt.Errorf("icacls %s: %v: %s", dir, err, strings.TrimSpace(string(out)))
	}
	if empty(dir) {
		return nil
	}
	out, err := exec.Command("icacls", cmds[1]...).CombinedOutput()
	if err != nil {
		// A file whose ACL lets nobody change it: take ownership (SYSTEM may) and try again.
		_ = exec.Command("takeown", "/F", dir, "/R", "/D", "Y").Run()
		if out, err = exec.Command("icacls", cmds[1]...).CombinedOutput(); err != nil {
			return fmt.Errorf("icacls reset %s: %v: %s", dir, err, strings.TrimSpace(string(out)))
		}
	}
	return nil
}

func empty(dir string) bool {
	m, _ := filepath.Glob(filepath.Join(dir, "*"))
	return len(m) == 0
}
