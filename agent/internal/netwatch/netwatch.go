// Package netwatch is the agent's local network watchdog (phase 15).
//
// A network adapter that stays down after a switch reboot (seen with Intel X710 ports on a Hyper-V
// host) is fixed by restarting the adapter. While the agent is connected, Stackpilot does that
// through its recovery rules, within the policy. When the agent cannot reach Stackpilot (often
// because of that very adapter), it can do it itself: Stackpilot pushes a small configuration with
// the adapters to watch and the limits, and the watchdog restarts a watched adapter that
//
//   - was up before (seen up by this agent),
//   - has had no link for DownMinutes,
//   - while the agent has also been offline for DownMinutes,
//
// at most MaxPerDay times per adapter per 24 hours and not within CooldownMinutes of the last try.
// An adapter that is switched off (disabled) is never touched. What it did is kept on disk and
// reported to Stackpilot when the agent is back (netwatch.status / netwatch.ack).
package netwatch

import (
	"context"
	"encoding/json"
	"log"
	"os"
	"path"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/rhisijmuiden/stackpilot/agent/internal/config"
)

// Adapter is a physical network adapter with its link state.
type Adapter struct {
	Name        string `json:"name"`
	Description string `json:"description"`
	Status      string `json:"status"` // up | down | disabled | absent | unknown
	Speed       string `json:"speed,omitempty"`
	MAC         string `json:"mac,omitempty"`
	Wireless    bool   `json:"wireless,omitempty"`
}

// Config is pushed by Stackpilot (netwatch.apply).
type Config struct {
	Enabled         bool     `json:"enabled"`
	Adapters        []string `json:"adapters"` // patterns on name or description; empty = every wired adapter
	DownMinutes     int      `json:"down_minutes"`
	CooldownMinutes int      `json:"cooldown_minutes"`
	MaxPerDay       int      `json:"max_per_day"`
	Hash            string   `json:"hash"`
}

// Action is one restart the watchdog did by itself.
type Action struct {
	At          time.Time `json:"at"`
	Adapter     string    `json:"adapter"`
	Description string    `json:"description,omitempty"`
	Before      string    `json:"before"`
	After       string    `json:"after"`
	OK          bool      `json:"ok"`
	Error       string    `json:"error,omitempty"`
}

type stored struct {
	Config  Config               `json:"config"`
	SeenUp  map[string]time.Time `json:"seen_up"`
	Actions []Action             `json:"actions"`
}

// Hooks are replaced in tests.
var (
	ListAdapters   = listAdapters
	RestartAdapter = restartAdapter
	Interval       = 30 * time.Second
)

const maxActions = 50

// Watcher runs the watchdog. OfflineFor tells how long the agent has been without a connection
// to Stackpilot (0 while connected).
type Watcher struct {
	OfflineFor func() time.Duration

	mu        sync.Mutex
	st        stored
	downSince map[string]time.Time
	tries     map[string][]time.Time
	file      string
}

// New loads the stored configuration (it survives restarts of the agent).
func New(offlineFor func() time.Duration) *Watcher {
	w := &Watcher{OfflineFor: offlineFor, downSince: map[string]time.Time{}, tries: map[string][]time.Time{},
		file: filepath.Join(config.StateDir, "netwatch.json")}
	if b, err := os.ReadFile(w.file); err == nil {
		_ = json.Unmarshal(b, &w.st)
	}
	if w.st.SeenUp == nil {
		w.st.SeenUp = map[string]time.Time{}
	}
	for _, a := range w.st.Actions {
		w.tries[a.Adapter] = append(w.tries[a.Adapter], a.At)
	}
	return w
}

func (w *Watcher) save() {
	b, _ := json.MarshalIndent(w.st, "", "  ")
	if err := config.WriteFileAtomic(w.file, b, 0o600); err != nil {
		log.Printf("netwatch: saving state: %v", err)
	}
}

// Apply stores a new configuration.
func (w *Watcher) Apply(c Config) Config {
	c.DownMinutes = clamp(c.DownMinutes, 2, 1440, 3)
	c.CooldownMinutes = clamp(c.CooldownMinutes, 5, 10080, 15)
	c.MaxPerDay = clamp(c.MaxPerDay, 1, 20, 3)
	if len(c.Adapters) > 30 {
		c.Adapters = c.Adapters[:30]
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	w.st.Config = c
	w.save()
	log.Printf("netwatch: configuration %s (enabled=%v, adapters=%v)", c.Hash, c.Enabled, c.Adapters)
	return c
}

// Status is what Stackpilot reads: the configuration it has and the actions not acknowledged yet.
func (w *Watcher) Status() map[string]any {
	w.mu.Lock()
	defer w.mu.Unlock()
	acts := append([]Action{}, w.st.Actions...)
	return map[string]any{"hash": w.st.Config.Hash, "enabled": w.st.Config.Enabled, "actions": acts}
}

// Ack removes the actions up to and including until (Stackpilot recorded them).
func (w *Watcher) Ack(until time.Time) int {
	w.mu.Lock()
	defer w.mu.Unlock()
	keep := w.st.Actions[:0]
	n := 0
	for _, a := range w.st.Actions {
		if !a.At.After(until) {
			n++
			continue
		}
		keep = append(keep, a)
	}
	w.st.Actions = keep
	w.save()
	return n
}

// Run checks every Interval until ctx ends.
func (w *Watcher) Run(ctx context.Context) {
	t := time.NewTicker(Interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			w.Check(ctx, time.Now())
		}
	}
}

// Check is one round of the watchdog.
func (w *Watcher) Check(ctx context.Context, now time.Time) {
	w.mu.Lock()
	cfg := w.st.Config
	w.mu.Unlock()
	if !cfg.Enabled {
		return
	}
	list, err := ListAdapters(ctx)
	if err != nil {
		log.Printf("netwatch: listing adapters: %v", err)
		return
	}
	down := time.Duration(cfg.DownMinutes) * time.Minute
	offline := time.Duration(0)
	if w.OfflineFor != nil {
		offline = w.OfflineFor()
	}
	for _, a := range list {
		if a.Wireless || !Match(cfg.Adapters, a) {
			continue
		}
		w.mu.Lock()
		switch a.Status {
		case "up":
			delete(w.downSince, a.Name)
			if last, ok := w.st.SeenUp[a.Name]; !ok || now.Sub(last) > time.Hour {
				w.st.SeenUp[a.Name] = now
				w.save()
			}
			w.mu.Unlock()
			continue
		case "down":
		default: // disabled, absent, unknown: never touched
			delete(w.downSince, a.Name)
			w.mu.Unlock()
			continue
		}
		if _, seen := w.st.SeenUp[a.Name]; !seen {
			w.mu.Unlock()
			continue
		}
		since, ok := w.downSince[a.Name]
		if !ok {
			w.downSince[a.Name] = now
			w.mu.Unlock()
			continue
		}
		if now.Sub(since) < down || offline < down || !w.mayTry(a.Name, cfg, now) {
			w.mu.Unlock()
			continue
		}
		w.tries[a.Name] = append(w.tries[a.Name], now)
		w.mu.Unlock()

		log.Printf("netwatch: %s (%s) has had no link for %s while offline for %s: restarting it",
			a.Name, a.Description, now.Sub(since).Round(time.Second), offline.Round(time.Second))
		after, rerr := RestartAdapter(ctx, a.Name)
		act := Action{At: now.UTC(), Adapter: a.Name, Description: a.Description, Before: a.Status, After: after,
			OK: rerr == nil && after == "up"}
		if rerr != nil {
			act.Error = rerr.Error()
		}
		log.Printf("netwatch: restarted %s: now %s (err=%v)", a.Name, after, rerr)
		w.mu.Lock()
		w.st.Actions = append(w.st.Actions, act)
		if len(w.st.Actions) > maxActions {
			w.st.Actions = w.st.Actions[len(w.st.Actions)-maxActions:]
		}
		if act.OK {
			delete(w.downSince, a.Name)
		} else {
			w.downSince[a.Name] = now // the next try waits for the cooldown and DownMinutes again
		}
		w.save()
		w.mu.Unlock()
	}
}

// mayTry applies the cooldown and the maximum per 24 hours (w.mu held).
func (w *Watcher) mayTry(name string, cfg Config, now time.Time) bool {
	var recent []time.Time
	for _, t := range w.tries[name] {
		if now.Sub(t) < 24*time.Hour {
			recent = append(recent, t)
		}
	}
	w.tries[name] = recent
	if len(recent) >= cfg.MaxPerDay {
		return false
	}
	if n := len(recent); n > 0 && now.Sub(recent[n-1]) < time.Duration(cfg.CooldownMinutes)*time.Minute {
		return false
	}
	return true
}

// Match tells whether an adapter matches one of the patterns (name or description, * and ?,
// case-insensitive). No patterns match every adapter.
func Match(patterns []string, a Adapter) bool {
	if len(patterns) == 0 {
		return true
	}
	name, desc := strings.ToLower(a.Name), strings.ToLower(a.Description)
	for _, p := range patterns {
		p = strings.ToLower(strings.TrimSpace(p))
		if p == "" {
			continue
		}
		if ok, _ := path.Match(p, name); ok {
			return true
		}
		if desc != "" {
			if ok, _ := path.Match(p, desc); ok {
				return true
			}
		}
	}
	return false
}

func clamp(v, lo, hi, def int) int {
	if v == 0 {
		return def
	}
	return max(lo, min(hi, v))
}
