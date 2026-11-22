package ops

import (
	"testing"
	"time"
)

func TestParseSince(t *testing.T) {
	now := time.Date(2026, 11, 22, 12, 0, 0, 0, time.UTC)
	cases := map[string]time.Time{
		"1 hour ago":       now.Add(-time.Hour),
		"30 min ago":       now.Add(-30 * time.Minute),
		"2 days ago":       now.Add(-48 * time.Hour),
		"today":            time.Date(2026, 11, 22, 0, 0, 0, 0, time.UTC),
		"2026-11-21 09:30": time.Date(2026, 11, 21, 9, 30, 0, 0, time.UTC),
		"2026-11-20":       time.Date(2026, 11, 20, 0, 0, 0, 0, time.UTC),
	}
	for in, want := range cases {
		got, err := ParseSince(in, now)
		if err != nil || !got.Equal(want) {
			t.Errorf("%q: got %v %v, want %v", in, got, err, want)
		}
	}
	if _, err := ParseSince("whenever", now); err == nil {
		t.Error("nonsense accepted")
	}
}

func TestGlobMatch(t *testing.T) {
	if !GlobMatch("nginx*", "nginx-common") || GlobMatch("nginx", "nginx-common") || !GlobMatch("*sql server*", "microsoft sql server 2022") {
		t.Fatal("glob")
	}
}
