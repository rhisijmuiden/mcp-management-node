package ops

import (
	"time"

	"github.com/rhisijmuiden/stackpilot/agent/internal/restartlog"
)

// Event is one entry of the Windows Event Log.
type Event struct {
	Time     string `json:"time"`
	Log      string `json:"log"`
	Level    string `json:"level"`
	Provider string `json:"provider"`
	ID       int    `json:"id"`
	Message  string `json:"message"`
	// ExpectedRestart is the version the agent restarted into when this Service Control Manager
	// event ("terminated unexpectedly") was the agent's own restart after a self-update.
	ExpectedRestart string `json:"expected_restart,omitempty"`
	Note            string `json:"note,omitempty"`
}

// AgentDisplayName is the display name of the agent service as the Service Control Manager
// writes it in its messages.
const AgentDisplayName = "Stackpilot agent"

// markUpdateRestarts marks the agent's own restarts after a self-update, so that they are not
// read as crashes.
func markUpdateRestarts(evs []Event, restarts []restartlog.Entry) {
	if len(restarts) == 0 {
		return
	}
	for i := range evs {
		e := &evs[i]
		if !restartlog.IsAgentServiceEvent(e.Provider, e.ID, e.Message, AgentDisplayName) {
			continue
		}
		t, err := time.Parse(time.RFC3339Nano, e.Time)
		if err != nil {
			continue
		}
		if r, ok := restartlog.Match(restarts, t); ok {
			e.ExpectedRestart = r.Version
			e.Note = restartlog.Note(r)
		}
	}
}
