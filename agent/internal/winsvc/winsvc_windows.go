//go:build windows

package winsvc

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"
)

const (
	Name        = "mmn-agent"
	DisplayName = "Stackpilot agent"
	Description = "Connects this server to Stackpilot over an outgoing mutual-TLS connection."
)

// IsService reports whether the process was started by the Service Control Manager.
func IsService() bool {
	ok, err := svc.IsWindowsService()
	return err == nil && ok
}

type handler struct{ run func(ctx context.Context) }

var (
	stopMu   sync.Mutex
	stopSelf context.CancelFunc // stops the running service cleanly
)

func (h *handler) Execute(args []string, req <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	status <- svc.Status{State: svc.StartPending}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	stopMu.Lock()
	stopSelf = cancel
	stopMu.Unlock()
	done := make(chan struct{})
	go func() { h.run(ctx); close(done) }()
	status <- svc.Status{State: svc.Running, Accepts: svc.AcceptStop | svc.AcceptShutdown}
	for {
		select {
		case <-done:
			status <- svc.Status{State: svc.StopPending}
			return false, 0
		case c := <-req:
			switch c.Cmd {
			case svc.Interrogate:
				status <- c.CurrentStatus
			case svc.Stop, svc.Shutdown:
				status <- svc.Status{State: svc.StopPending}
				cancel()
				select {
				case <-done:
				case <-time.After(20 * time.Second):
				}
				return false, 0
			}
		}
	}
}

const (
	detachedProcess        = 0x00000008
	createNewProcessGroup  = 0x00000200
	createBreakawayFromJob = 0x01000000
	createNoWindow         = 0x08000000
)

// Restart stops the service cleanly and has a detached helper start it again (after a
// self-update). It returns false when the helper could not be started.
func Restart() bool {
	stopMu.Lock()
	stop := stopSelf
	stopMu.Unlock()
	if stop == nil {
		return false
	}
	ps := filepath.Join(os.Getenv("SystemRoot"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
	start := func(flags uint32) error {
		cmd := exec.Command(ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", RestartScript(Name))
		cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: flags, HideWindow: true}
		if err := cmd.Start(); err != nil {
			return err
		}
		return cmd.Process.Release()
	}
	base := uint32(detachedProcess | createNewProcessGroup | createNoWindow)
	if err := start(base | createBreakawayFromJob); err != nil {
		if err := start(base); err != nil {
			log.Printf("restart helper: %v", err)
			return false
		}
	}
	// Safety net: if the service does not stop in time, exit with an error so the service
	// recovery restarts it.
	go func() { time.Sleep(60 * time.Second); os.Exit(1) }()
	stop()
	return true
}

// Run runs fn as the service until Windows stops it. Logging goes to agent.log in dir.
func Run(dir string, fn func(ctx context.Context)) error {
	if f := openLog(dir); f != nil {
		log.SetOutput(f)
		defer f.Close()
	}
	setRecovery()
	return svc.Run(Name, &handler{run: fn})
}

func openLog(dir string) io.WriteCloser {
	_ = os.MkdirAll(dir, 0o700)
	p := filepath.Join(dir, "agent.log")
	if fi, err := os.Stat(p); err == nil && fi.Size() > 5<<20 {
		_ = os.Rename(p, p+".1")
	}
	f, err := os.OpenFile(p, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return nil
	}
	return f
}

// setRecovery makes Windows restart the agent when it exits with an error (also after an update).
func setRecovery() {
	m, err := mgr.Connect()
	if err != nil {
		return
	}
	defer m.Disconnect()
	s, err := m.OpenService(Name)
	if err != nil {
		return
	}
	defer s.Close()
	acts := []mgr.RecoveryAction{{Type: mgr.ServiceRestart, Delay: 5 * time.Second}, {Type: mgr.ServiceRestart, Delay: 10 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 30 * time.Second}}
	_ = s.SetRecoveryActions(acts, 86400)
	_ = s.SetRecoveryActionsOnNonCrashFailures(true)
}

// Install registers the service (automatic start, LocalSystem) for exe and starts it.
func Install(exe string) error {
	m, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer m.Disconnect()
	if s, err := m.OpenService(Name); err == nil {
		s.Close()
		return errors.New("the service is installed already")
	}
	s, err := m.CreateService(Name, exe, mgr.Config{DisplayName: DisplayName, Description: Description,
		StartType: mgr.StartAutomatic, DelayedAutoStart: false}, "run")
	if err != nil {
		return err
	}
	defer s.Close()
	setRecovery()
	return s.Start()
}

// Uninstall stops and removes the service.
func Uninstall() error {
	m, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer m.Disconnect()
	s, err := m.OpenService(Name)
	if err != nil {
		return fmt.Errorf("service not found: %w", err)
	}
	defer s.Close()
	_, _ = s.Control(svc.Stop)
	return s.Delete()
}
