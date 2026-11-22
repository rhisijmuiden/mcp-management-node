package transfer

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPackUnpack(t *testing.T) {
	src := t.TempDir()
	os.MkdirAll(filepath.Join(src, "site", "img"), 0o755)
	os.WriteFile(filepath.Join(src, "site", "index.html"), []byte("<h1>hi</h1>"), 0o644)
	os.WriteFile(filepath.Join(src, "site", "img", "a.png"), bytes.Repeat([]byte{1}, 5000), 0o600)
	os.Symlink("index.html", filepath.Join(src, "site", "home.html"))
	os.Symlink("/etc/passwd", filepath.Join(src, "site", "evil"))
	var buf bytes.Buffer
	log := func(string) {}
	r, err := Pack(context.Background(), &buf, []string{filepath.Join(src, "site")}, log)
	if err != nil || r.Files != 2 {
		t.Fatalf("%v %+v", err, r)
	}
	dst := t.TempDir()
	data := buf.Bytes()
	u, err := Unpack(context.Background(), bytes.NewReader(data), dst, false, log)
	if err != nil || u.Files != 2 || u.Bytes != 5011 {
		t.Fatalf("%v %+v", err, u)
	}
	if b, _ := os.ReadFile(filepath.Join(dst, "site", "index.html")); string(b) != "<h1>hi</h1>" {
		t.Fatal("content")
	}
	if fi, _ := os.Stat(filepath.Join(dst, "site", "img", "a.png")); fi.Mode().Perm() != 0o600 {
		t.Fatal("mode", fi.Mode())
	}
	if l, _ := os.Readlink(filepath.Join(dst, "site", "home.html")); l != "index.html" {
		t.Fatal("symlink inside should be kept")
	}
	if _, err := os.Lstat(filepath.Join(dst, "site", "evil")); err == nil {
		t.Fatal("symlink outside should be skipped")
	}
	// Existing files are not replaced without overwrite.
	if _, err := Unpack(context.Background(), bytes.NewReader(data), dst, false, log); err == nil || !strings.Contains(err.Error(), "exists") {
		t.Fatal(err)
	}
	if _, err := Unpack(context.Background(), bytes.NewReader(data), dst, true, log); err != nil {
		t.Fatal(err)
	}
}

func TestUnpackRefusesTraversal(t *testing.T) {
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	tw.WriteHeader(&tar.Header{Name: "../escape.txt", Mode: 0o644, Size: 1, Typeflag: tar.TypeReg})
	tw.Write([]byte("x"))
	tw.Close()
	gz.Close()
	if _, err := Unpack(context.Background(), &buf, t.TempDir(), true, func(string) {}); err == nil {
		t.Fatal("traversal accepted")
	}
}
