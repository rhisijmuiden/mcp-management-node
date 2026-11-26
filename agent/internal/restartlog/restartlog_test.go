package restartlog

import (
	"testing"
	"time"
)

func TestRecordLoadMatch(t *testing.T) {
	dir := t.TempDir()
	if Load(dir) != nil {
		t.Fatal("expected nothing recorded")
	}
	at := time.Date(2026, 11, 26, 18, 47, 12, 0, time.UTC)
	for i := 0; i < keep+5; i++ {
		if err := Record(dir, "0.0.1", at.Add(-time.Duration(keep+5-i)*time.Hour)); err != nil {
			t.Fatal(err)
		}
	}
	if err := Record(dir, "0.27.0", at); err != nil {
		t.Fatal(err)
	}
	es := Load(dir)
	if len(es) != keep {
		t.Fatalf("kept %d entries, want %d", len(es), keep)
	}
	if e, ok := Match(es, at.Add(30*time.Second)); !ok || e.Version != "0.27.0" {
		t.Fatalf("no match 30 s after the restart: %+v %v", e, ok)
	}
	if _, ok := Match(es, at.Add(Window+time.Minute)); ok {
		t.Fatal("matched an event long after the restart")
	}
}
