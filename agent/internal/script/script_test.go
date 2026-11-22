package script

import (
	"context"
	"strings"
	"testing"
	"time"
)

func TestRunStreamsCleanLines(t *testing.T) {
	var lines []string
	r, err := Run(context.Background(), "echo -e '\\e[32mhello\\e[0m'; printf 'a\\rb\\n'; echo \"$FOO\"",
		map[string]string{"FOO": "bar"}, 10*time.Second, func(s string) { lines = append(lines, s) })
	if err != nil || r.ExitCode != 0 {
		t.Fatal(err)
	}
	if strings.Join(lines, "|") != "hello|a|b|bar" {
		t.Fatalf("got %q", lines)
	}
}

func TestRunExitCode(t *testing.T) {
	r, err := Run(context.Background(), "exit 3", nil, 10*time.Second, func(string) {})
	if err == nil || r.ExitCode != 3 {
		t.Fatalf("bad: %v %+v", err, r)
	}
	if _, err := Run(context.Background(), "true", map[string]string{"BAD-KEY": "x"}, time.Second, func(string) {}); err == nil {
		t.Fatal("bad env key accepted")
	}
}
