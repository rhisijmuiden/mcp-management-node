package client

import (
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/rhisijmuiden/stackpilot/agent/internal/winsvc"
)

func withRestartSettings(t *testing.T, drain time.Duration, hook func() bool) {
	t.Helper()
	oldDrain, oldDelay, oldHook := DrainTimeout, restartDelay, RestartHook
	DrainTimeout, restartDelay, RestartHook = drain, 0, hook
	t.Cleanup(func() { DrainTimeout, restartDelay, RestartHook = oldDrain, oldDelay, oldHook })
}

// The update waits for a running command to finish before the agent restarts.
func TestRestartWaitsForRunningRequests(t *testing.T) {
	var exited atomic.Int32
	a := &Agent{exit: func(int) { exited.Add(1) }}
	withRestartSettings(t, 10*time.Second, nil)
	a.inflight.Store(2) // the update request itself + one running command
	done := make(chan struct{})
	go func() { a.restartAfterUpdate("9.9.9"); close(done) }()
	time.Sleep(500 * time.Millisecond)
	if exited.Load() != 0 {
		t.Fatal("agent restarted while a command was still running")
	}
	a.inflight.Add(-1) // the command finishes
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("agent did not restart after the command finished")
	}
	if exited.Load() != 1 {
		t.Fatalf("exit called %d times", exited.Load())
	}
}

// A request that never ends does not block the update forever.
func TestRestartAfterDrainTimeout(t *testing.T) {
	var code atomic.Int32
	code.Store(-1)
	a := &Agent{exit: func(c int) { code.Store(int32(c)) }}
	withRestartSettings(t, 300*time.Millisecond, nil)
	a.inflight.Store(3)
	a.restartAfterUpdate("9.9.9")
	if code.Load() != int32(RestartExitCode) {
		t.Fatalf("exit code %d, want %d", code.Load(), RestartExitCode)
	}
}

// With a restart hook (the Windows service) the agent does not exit with an error.
func TestRestartUsesHook(t *testing.T) {
	var exited, hooked atomic.Int32
	a := &Agent{exit: func(int) { exited.Add(1) }}
	withRestartSettings(t, time.Second, func() bool { hooked.Add(1); return true })
	a.inflight.Store(1)
	a.restartAfterUpdate("9.9.9")
	if hooked.Load() != 1 || exited.Load() != 0 {
		t.Fatalf("hook %d, exit %d; want the hook only", hooked.Load(), exited.Load())
	}
}

// When the hook cannot arrange a restart the agent falls back to exiting.
func TestRestartHookFailureFallsBackToExit(t *testing.T) {
	var exited atomic.Int32
	a := &Agent{exit: func(int) { exited.Add(1) }}
	withRestartSettings(t, time.Second, func() bool { return false })
	a.inflight.Store(1)
	a.restartAfterUpdate("9.9.9")
	if exited.Load() != 1 {
		t.Fatal("agent did not exit after the hook failed")
	}
}

func TestWindowsRestartScript(t *testing.T) {
	s := winsvc.RestartScript("mmn-agent")
	for _, want := range []string{"$n='mmn-agent'", "Get-Service -Name $n", "'Stopped'", "Start-Service -Name $n"} {
		if !strings.Contains(s, want) {
			t.Errorf("script lacks %q: %s", want, s)
		}
	}
}
