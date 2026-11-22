package client

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

// fetchRequest downloads a file from the app (a custom CT template) into a Proxmox VE storage.
type fetchRequest struct {
	URL    string `json:"url"`
	SHA256 string `json:"sha256"`
	VolID  string `json:"volid"` // e.g. local:vztmpl/my-template.tar.zst
	Size   int64  `json:"size"`
}

var volidRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*:(vztmpl|iso)/[A-Za-z0-9][A-Za-z0-9_.+-]*$`)

type progressWriter struct {
	total, done, step int64
	next              int64
	line              func(string)
}

func (p *progressWriter) Write(b []byte) (int, error) {
	p.done += int64(len(b))
	if p.done >= p.next {
		if p.total > 0 {
			p.line(fmt.Sprintf("downloaded %d%% (%d MiB of %d MiB)", p.done*100/p.total, p.done>>20, p.total>>20))
		} else {
			p.line(fmt.Sprintf("downloaded %d MiB", p.done>>20))
		}
		p.next += p.step
	}
	return len(b), nil
}

func fetchFile(ctx context.Context, hc *http.Client, q fetchRequest, line func(string)) (any, error) {
	if !volidRE.MatchString(q.VolID) || len(q.SHA256) != 64 || q.URL == "" {
		return nil, errors.New("url, sha256 and a valid volid are required")
	}
	pctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	out, err := exec.CommandContext(pctx, "pvesm", "path", q.VolID).Output()
	cancel()
	if err != nil {
		return nil, fmt.Errorf("pvesm path %s: %w", q.VolID, err)
	}
	dst := strings.TrimSpace(string(out))
	if !strings.HasPrefix(dst, "/") {
		return nil, errors.New("this storage has no local path")
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, q.URL, nil)
	if err != nil {
		return nil, err
	}
	resp, err := hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("download failed: HTTP %d", resp.StatusCode)
	}
	tmp := dst + ".mmn-part"
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, err
	}
	h := sha256.New()
	total := resp.ContentLength
	if total <= 0 {
		total = q.Size
	}
	pw := &progressWriter{total: total, step: 64 << 20, next: 64 << 20, line: line}
	_, err = io.Copy(io.MultiWriter(f, h, pw), io.LimitReader(resp.Body, 20<<30))
	f.Close()
	if err != nil {
		os.Remove(tmp)
		return nil, err
	}
	if got := hex.EncodeToString(h.Sum(nil)); got != strings.ToLower(q.SHA256) {
		os.Remove(tmp)
		return nil, fmt.Errorf("checksum mismatch (got %s)", got)
	}
	if err := os.Rename(tmp, dst); err != nil {
		return nil, err
	}
	line("saved as " + q.VolID)
	return map[string]any{"volid": q.VolID, "path": dst, "size": pw.done}, nil
}
