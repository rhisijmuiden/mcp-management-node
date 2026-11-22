package governor

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func fakeSys(t *testing.T, cpus int) string {
	root := t.TempDir()
	for i := 0; i < cpus; i++ {
		d := filepath.Join(root, "cpu"+string(rune('0'+i)), "cpufreq")
		os.MkdirAll(d, 0o755)
		os.WriteFile(filepath.Join(d, "scaling_governor"), []byte("schedutil\n"), 0o644)
		os.WriteFile(filepath.Join(d, "scaling_available_governors"), []byte("performance schedutil powersave\n"), 0o644)
		os.WriteFile(filepath.Join(d, "scaling_driver"), []byte("acpi-cpufreq\n"), 0o644)
	}
	return root
}

func TestGetSet(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("linux only")
	}
	SysRoot = fakeSys(t, 4)
	info, err := Get()
	if err != nil {
		t.Fatal(err)
	}
	if info.Current != "schedutil" || len(info.Available) != 3 || info.Driver != "acpi-cpufreq" {
		t.Fatalf("unexpected %+v", info)
	}
	info, err = Set("performance")
	if err != nil || info.Current != "performance" {
		t.Fatalf("set: %v %+v", err, info)
	}
	if _, err := Set("ondemand"); err == nil {
		t.Fatal("expected error for unavailable governor")
	}
	SysRoot = t.TempDir()
	if _, err := Get(); err != ErrUnsupported {
		t.Fatalf("expected ErrUnsupported, got %v", err)
	}
}
