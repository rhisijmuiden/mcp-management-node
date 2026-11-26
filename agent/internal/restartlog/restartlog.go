// Package restartlog remembers when the agent restarted itself after a self-update.
//
// On Windows the agent exits with an error after an update so that the service recovery starts
// the new version; the Service Control Manager logs that as event 7031 "terminated unexpectedly".
// With this record the agent can mark those events as expected when it returns the event log,
// so they are not mistaken for crashes.
package restartlog

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Entry is one restart for an update.
type Entry struct {
	At      time.Time `json:"at"`
	Version string    `json:"version"`
}

// Window is how long after a recorded restart an SCM event still belongs to it (the recovery
// waits 5 to 30 seconds; the safety task starts the service after 90 seconds at the latest).
const Window = 3 * time.Minute

const keep = 200

func file(dir string) string { return filepath.Join(dir, "update-restarts.json") }

// Load returns the recorded restarts, oldest first. A missing or broken file gives none.
func Load(dir string) []Entry {
	b, err := os.ReadFile(file(dir))
	if err != nil {
		return nil
	}
	var es []Entry
	if json.Unmarshal(b, &es) != nil {
		return nil
	}
	return es
}

// Record adds a restart to version at the given time (the last 200 are kept).
func Record(dir, version string, at time.Time) error {
	es := append(Load(dir), Entry{At: at.UTC(), Version: version})
	if len(es) > keep {
		es = es[len(es)-keep:]
	}
	b, err := json.Marshal(es)
	if err != nil {
		return err
	}
	tmp := file(dir) + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, file(dir))
}

// Match returns the restart that an event at t belongs to: t lies in [restart, restart+Window].
func Match(es []Entry, t time.Time) (Entry, bool) {
	for i := len(es) - 1; i >= 0; i-- {
		d := t.Sub(es[i].At)
		if d >= -5*time.Second && d <= Window {
			return es[i], true
		}
	}
	return Entry{}, false
}

// IsAgentServiceEvent reports whether a Service Control Manager event is about the agent service
// stopping (7031 terminated unexpectedly, 7034 terminated unexpectedly without recovery, 7024
// terminated with a service-specific error).
func IsAgentServiceEvent(provider string, id int, message, displayName string) bool {
	if provider != "Service Control Manager" {
		return false
	}
	if id != 7031 && id != 7034 && id != 7024 && id != 7023 {
		return false
	}
	return strings.Contains(message, displayName)
}

// Note is the explanation added to such an event, in English (the app translates it).
func Note(e Entry) string {
	return "Expected: the agent restarted itself to run version " + e.Version + " after a self-update; this is not a crash."
}
