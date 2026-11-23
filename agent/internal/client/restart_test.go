package client

import (
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/rhisijmuiden/stackpilot/agent/internal/winsvc"
)

func withRestartSettings(t *testing.T, drain time.Duration, before func()) {
	t.Helper()
	oldDrain, oldDelay, oldBefore := DrainTimeout, restartDelay, BeforeRestart
	DrainTimeout, restartDelay, BeforeRestart = drain, 0, before
	t.Cleanup(func() { DrainTimeout, restartDelay, BeforeRestart = oldDrain, oldDelay, oldBefore })
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

// The agent always exits so the service manager restarts it (on Windows: the service recovery),
// after arming the safety net. A clean stop that relies on a helper process to start the service
// again left every Windows agent stopped after the 0.8.0 update.
func TestRestartArmsSafetyNetThenExits(t *testing.T) {
	var order []string
	var code atomic.Int32
	code.Store(-1)
	a := &Agent{exit: func(c int) { order = append(order, "exit"); code.Store(int32(c)) }}
	withRestartSettings(t, time.Second, func() { order = append(order, "arm") })
	a.inflight.Store(1)
	a.restartAfterUpdate("9.9.9")
	if strings.Join(order, ",") != "arm,exit" || code.Load() != int32(RestartExitCode) {
		t.Fatalf("order %v code %d; want arm then exit(%d)", order, code.Load(), RestartExitCode)
	}
}

func TestWindowsRestartTaskScript(t *testing.T) {
	s := winsvc.RestartTaskScript("mmn-agent", 90)
	for _, want := range []string{
		"Register-ScheduledTask -TaskName 'Stackpilot agent restart'",
		"-UserId 'SYSTEM'",
		"AddSeconds(90)",
		"Get-Service -Name ''mmn-agent''", // quoted inside the single-quoted $cmd
		"Start-Service -Name ''mmn-agent''",
		"-Force",
		"'armed'",
	} {
		if !strings.Contains(s, want) {
			t.Errorf("script lacks %q:\n%s", want, s)
		}
	}
	if !strings.Contains(winsvc.RemoveTaskScript(), "Unregister-ScheduledTask -TaskName 'Stackpilot agent restart'") {
		t.Error("remove script")
	}
}
