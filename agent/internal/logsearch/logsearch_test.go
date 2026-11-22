package logsearch

import (
	"compress/gzip"
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestSearch(t *testing.T) {
	d := t.TempDir()
	os.MkdirAll(filepath.Join(d, "nginx"), 0o755)
	os.WriteFile(filepath.Join(d, "nginx", "error.log"), []byte("ok\n2026/11/22 [error] upstream timed out\nfine\n"), 0o644)
	os.WriteFile(filepath.Join(d, "bin.dat"), []byte("error\x00\x01"), 0o644)
	f, _ := os.Create(filepath.Join(d, "nginx", "error.log.2.gz"))
	gz := gzip.NewWriter(f)
	gz.Write([]byte("old ERROR line\n"))
	gz.Close()
	f.Close()
	r, err := Search(context.Background(), Query{Paths: []string{d}, Pattern: "error", Gzip: true})
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Matches) != 2 || r.Matches[0].Line == 0 {
		t.Fatalf("%+v", r)
	}
	r, _ = Search(context.Background(), Query{Paths: []string{filepath.Join(d, "nginx", "*.log")}, Pattern: "timed out", MaxMatches: 1})
	if len(r.Matches) != 1 || !r.Truncated || r.Matches[0].Line != 2 {
		t.Fatalf("%+v", r)
	}
	if _, err := Search(context.Background(), Query{Paths: []string{"relative"}, Pattern: "x"}); err == nil {
		t.Fatal("relative path accepted")
	}
}
