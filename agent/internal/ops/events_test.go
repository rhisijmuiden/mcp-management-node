package ops

import (
	"testing"
	"time"

	"github.com/rhisijmuiden/stackpilot/agent/internal/restartlog"
)

// The 7031 event Windows logs when the agent restarts for an update is marked as expected; a
// crash at another time, or another service, is left alone.
func TestMarkUpdateRestarts(t *testing.T) {
	at := time.Date(2026, 11, 26, 18, 47, 12, 0, time.UTC)
	restarts := []restartlog.Entry{{At: at, Version: "0.27.0"}}
	msg := "The Stackpilot agent service terminated unexpectedly. It has done this 49284 time(s)."
	evs := []Event{
		{Time: "2026-11-26T18:47:13.4042036Z", Provider: "Service Control Manager", ID: 7031, Message: msg},
		{Time: "2026-11-26T20:00:00.0000000Z", Provider: "Service Control Manager", ID: 7031, Message: msg},
		{Time: "2026-11-26T18:47:13.0000000Z", Provider: "Service Control Manager", ID: 7031, Message: "The Roeda_NG service terminated unexpectedly."},
		{Time: "2026-11-26T18:47:44.2168110Z", Provider: "Service Control Manager", ID: 7036, Message: "The Stackpilot agent service entered the running state."},
	}
	markUpdateRestarts(evs, restarts)
	if evs[0].ExpectedRestart != "0.27.0" || evs[0].Note == "" {
		t.Fatalf("update restart not marked: %+v", evs[0])
	}
	for _, e := range evs[1:] {
		if e.ExpectedRestart != "" {
			t.Fatalf("marked wrongly: %+v", e)
		}
	}
}
