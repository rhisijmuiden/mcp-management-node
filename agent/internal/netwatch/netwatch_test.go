package netwatch

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/rhisijmuiden/stackpilot/agent/internal/config"
)

func TestMatch(t *testing.T) {
	x710 := Adapter{Name: "SLOT 3 Port 1", Description: "Intel(R) Ethernet Controller X710 for 10GbE SFP+"}
	if !Match(nil, x710) || !Match([]string{"*X710*"}, x710) || !Match([]string{"slot 3 port ?"}, x710) {
		t.Fatal("expected a match")
	}
	if Match([]string{"Ethernet 2", "eno*"}, x710) {
		t.Fatal("unexpected match")
	}
}

func TestWatchdog(t *testing.T) {
	old := config.StateDir
	config.StateDir = t.TempDir()
	defer func() { config.StateDir = old }()
	state := "up"
	restarts := 0
	fixes := true
	ListAdapters = func(context.Context) ([]Adapter, error) {
		return []Adapter{
			{Name: "SLOT 3 Port 1", Description: "Intel X710", Status: state},
			{Name: "Ethernet 4", Description: "spare", Status: "down"}, // never seen up
			{Name: "Wi-Fi", Description: "wifi", Status: "down", Wireless: true},
			{Name: "Backup", Description: "off", Status: "disabled"},
		}, nil
	}
	RestartAdapter = func(_ context.Context, name string) (string, error) {
		if name != "SLOT 3 Port 1" {
			t.Fatalf("restarted %s", name)
		}
		restarts++
		if fixes {
			state = "up"
			return "up", nil
		}
		return "down", errors.New("no link")
	}
	defer func() { ListAdapters, RestartAdapter = listAdapters, restartAdapter }()

	offline := time.Duration(0)
	w := New(func() time.Duration { return offline })
	ctx := context.Background()
	now := time.Date(2026, 11, 24, 12, 0, 0, 0, time.UTC)

	// Switched off: nothing happens.
	w.Check(ctx, now)
	if restarts != 0 {
		t.Fatal("disabled watchdog acted")
	}
	w.Apply(Config{Enabled: true, Adapters: []string{"*X710*", "Ethernet 4"}, DownMinutes: 3, CooldownMinutes: 15, MaxPerDay: 2, Hash: "h1"})
	w.Check(ctx, now) // seen up
	state = "down"
	w.Check(ctx, now.Add(time.Minute))
	// Connected to Stackpilot: the server handles it, the agent waits.
	w.Check(ctx, now.Add(10*time.Minute))
	if restarts != 0 {
		t.Fatal("acted while connected")
	}
	offline = 10 * time.Minute
	w.Check(ctx, now.Add(11*time.Minute))
	if restarts != 1 || state != "up" {
		t.Fatalf("restarts=%d state=%s", restarts, state)
	}
	st := w.Status()
	acts := st["actions"].([]Action)
	if len(acts) != 1 || !acts[0].OK || acts[0].Adapter != "SLOT 3 Port 1" || st["hash"] != "h1" {
		t.Fatalf("status %+v", st)
	}

	// Loop protection: a failing fix is tried again only after the cooldown and at most twice a day.
	fixes = false
	state = "down"
	w.Check(ctx, now.Add(12*time.Minute))
	w.Check(ctx, now.Add(16*time.Minute)) // down 4 minutes, but within the cooldown of the first try
	if restarts != 1 {
		t.Fatalf("cooldown ignored: %d", restarts)
	}
	w.Check(ctx, now.Add(30*time.Minute))
	if restarts != 2 {
		t.Fatalf("expected a second try: %d", restarts)
	}
	w.Check(ctx, now.Add(60*time.Minute))
	w.Check(ctx, now.Add(90*time.Minute))
	if restarts != 2 {
		t.Fatalf("maximum per day ignored: %d", restarts)
	}
	if n := w.Ack(acts[0].At); n != 1 || len(w.Status()["actions"].([]Action)) != 1 {
		t.Fatalf("ack removed %d", n)
	}

	// The state survives a restart of the agent.
	w2 := New(nil)
	if w2.Status()["hash"] != "h1" || len(w2.Status()["actions"].([]Action)) != 1 {
		t.Fatalf("not restored: %+v", w2.Status())
	}
}
