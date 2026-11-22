package apt

import (
	"os"
	"path/filepath"
	"testing"
)

func TestParseUpgradable(t *testing.T) {
	out := `Listing...
pve-manager/stable 9.0.5 amd64 [upgradable from: 9.0.3]
libc6/trixie-security 2.41-12 amd64 [upgradable from: 2.41-11]

WARNING: apt does not have a stable CLI interface.
`
	p := ParseUpgradable(out)
	if len(p) != 2 {
		t.Fatalf("got %d packages: %+v", len(p), p)
	}
	if p[0].Name != "libc6" || p[0].Suite != "trixie-security" || p[0].Current != "2.41-11" || p[0].New != "2.41-12" {
		t.Fatalf("bad parse: %+v", p[0])
	}
	if p[1].Name != "pve-manager" || p[1].Arch != "amd64" {
		t.Fatalf("bad parse: %+v", p[1])
	}
	if len(ParseUpgradable("Listing...\n")) != 0 {
		t.Fatal("expected none")
	}
}

func TestRebootRequired(t *testing.T) {
	dir := t.TempDir()
	BootDir = dir
	for _, k := range []string{"6.14.8-2-pve", "6.14.11-1-pve", "6.8.12-9-pve"} {
		os.WriteFile(filepath.Join(dir, "vmlinuz-"+k), nil, 0o644)
	}
	RunningKernel = func() string { return "6.14.8-2-pve" }
	if !RebootRequired() {
		t.Fatal("newer kernel installed: reboot expected")
	}
	RunningKernel = func() string { return "6.14.11-1-pve" }
	if RebootRequired() {
		t.Fatal("running newest kernel: no reboot expected")
	}
}
