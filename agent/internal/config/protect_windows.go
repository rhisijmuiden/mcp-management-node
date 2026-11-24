//go:build windows

package config

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// Protect limits a folder to SYSTEM and the Administrators group (no inherited rights from
// %ProgramData%, where every user can read), for the keys, the state and the scripts that run.
func Protect(dir string) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	out, err := exec.Command("icacls", dir, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F",
		"*S-1-5-32-544:(OI)(CI)F", "/T", "/C", "/Q").CombinedOutput()
	if err != nil {
		return fmt.Errorf("icacls %s: %v: %s", dir, err, strings.TrimSpace(string(out)))
	}
	return nil
}
