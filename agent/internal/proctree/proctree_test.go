//go:build !windows

package proctree

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

// When the time-out passes, the processes the shell started stop too, and Wait returns even
// though a background process still held the output pipe.
func TestTimeoutKillsChildren(t *testing.T) {
	marker := filepath.Join(t.TempDir(), "still-running")
	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	cmd := exec.CommandContext(ctx, "/bin/sh", "-c", "(sleep 2; touch "+marker+") & sleep 30")
	Setup(cmd)
	var out testWriter
	cmd.Stdout = &out
	start := time.Now()
	_ = cmd.Run()
	if d := time.Since(start); d > 5*time.Second {
		t.Fatalf("Run took %s after the time-out", d)
	}
	time.Sleep(2500 * time.Millisecond)
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("a child process kept running after the time-out")
	}
}

type testWriter struct{ n int }

func (w *testWriter) Write(p []byte) (int, error) { w.n += len(p); return len(p), nil }
