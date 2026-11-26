//go:build windows

package winsvc

import (
	"log"

	"golang.org/x/sys/windows/svc/eventlog"
)

// EventSource is the Application log source the agent writes to.
const EventSource = "Stackpilot agent"

// UpdateRestartEventID is the Application log event the agent writes just before it restarts
// for a self-update (EventCreate message file: ids 1 to 1000).
const UpdateRestartEventID = 1000

// LogUpdateRestart writes an information event that the agent restarts on purpose, so that a
// person reading the event log sees why event 7031 follows.
func LogUpdateRestart(version string) {
	// Registers the source the first time; afterwards it reports that it exists already.
	_ = eventlog.InstallAsEventCreate(EventSource, eventlog.Info|eventlog.Warning|eventlog.Error)
	l, err := eventlog.Open(EventSource)
	if err != nil {
		log.Printf("event log: %v", err)
		return
	}
	defer l.Close()
	msg := "The Stackpilot agent restarts to run version " + version + " after a self-update. " +
		"The Service Control Manager logs this as event 7031 (terminated unexpectedly) and starts the new version; that is expected, not a crash."
	if err := l.Info(UpdateRestartEventID, msg); err != nil {
		log.Printf("event log: %v", err)
	}
}
