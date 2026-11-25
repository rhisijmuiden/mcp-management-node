package config

import (
	"path/filepath"
	"strings"
	"testing"
)

// Regression for 0.15.0: the (OI)(CI) grant must not be applied with /T (icacls refuses it on
// files after removing their inherited rights, which locked everyone out of config.json).
func TestProtectCommands(t *testing.T) {
	dir := filepath.Join("C:", "ProgramData", "mmn-agent")
	cmds := protectCommands(dir)
	if len(cmds) != 2 {
		t.Fatalf("got %d commands", len(cmds))
	}
	grant := strings.Join(cmds[0], " ")
	if cmds[0][0] != dir || !strings.Contains(grant, "/inheritance:r") ||
		!strings.Contains(grant, "*S-1-5-18:(OI)(CI)F") || !strings.Contains(grant, "*S-1-5-32-544:(OI)(CI)F") {
		t.Errorf("grant: %v", cmds[0])
	}
	for _, a := range cmds[0] {
		if strings.EqualFold(a, "/T") {
			t.Errorf("the grant with inheritance flags must only touch the folder: %v", cmds[0])
		}
	}
	reset := cmds[1]
	if reset[0] != filepath.Join(dir, "*") || reset[1] != "/reset" || !strings.Contains(strings.Join(reset, " "), "/T") {
		t.Errorf("reset: %v", reset)
	}
	for _, a := range reset {
		if strings.Contains(a, "(OI)") || strings.Contains(a, "/inheritance") {
			t.Errorf("the reset must only make the children inherit: %v", reset)
		}
	}
}
