// Package logsearch searches text log files (also rotated .gz files) for a pattern, newest
// files first, with limits on time, bytes and matches so a search never hurts the server.
package logsearch

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"time"
)

type Query struct {
	// Paths are files, directories (searched up to 4 levels deep) or globs like /var/log/nginx/*.log.
	Paths      []string `json:"paths"`
	Pattern    string   `json:"pattern"`     // regular expression (RE2)
	IgnoreCase *bool    `json:"ignore_case"` // default true
	MaxMatches int      `json:"max_matches"` // default 200, max 5000
	// ModifiedWithin skips files not changed in this many hours (0 = all files).
	ModifiedWithin int  `json:"modified_within_hours"`
	Gzip           bool `json:"include_gz"` // also search rotated *.gz files
}

type Match struct {
	File string `json:"file"`
	Line int    `json:"line"`
	Text string `json:"text"`
}

type Result struct {
	Matches      []Match  `json:"matches"`
	FilesScanned int      `json:"files_scanned"`
	BytesScanned int64    `json:"bytes_scanned"`
	Truncated    bool     `json:"truncated"`
	Stopped      string   `json:"stopped,omitempty"` // why the search stopped early
	Errors       []string `json:"errors,omitempty"`
}

var (
	MaxBytes    int64 = 2 << 30
	MaxDuration       = 90 * time.Second
)

// DefaultPaths are searched when the query names none.
func DefaultPaths() []string {
	if runtime.GOOS == "windows" {
		return []string{`C:\inetpub\logs\LogFiles`, `C:\ProgramData\mmn-agent`}
	}
	return []string{"/var/log"}
}

type file struct {
	path string
	mod  time.Time
	size int64
}

func collect(paths []string, since time.Time, gz bool) ([]file, []string) {
	var out []file
	var errs []string
	seen := map[string]bool{}
	add := func(p string, info fs.FileInfo) {
		if seen[p] || !info.Mode().IsRegular() || (!since.IsZero() && info.ModTime().Before(since)) {
			return
		}
		lower := strings.ToLower(p)
		if strings.HasSuffix(lower, ".gz") && !gz {
			return
		}
		for _, skip := range []string{".xz", ".zst", ".bz2", ".zip", "/journal/", `\journal\`, "lastlog", "wtmp", "btmp", ".journal"} {
			if strings.Contains(lower, skip) {
				return
			}
		}
		seen[p] = true
		out = append(out, file{p, info.ModTime(), info.Size()})
	}
	for _, p := range paths {
		matches := []string{p}
		if strings.ContainsAny(p, "*?[") {
			m, err := filepath.Glob(p)
			if err != nil {
				errs = append(errs, p+": "+err.Error())
				continue
			}
			matches = m
		}
		for _, m := range matches {
			info, err := os.Stat(m)
			if err != nil {
				errs = append(errs, err.Error())
				continue
			}
			if !info.IsDir() {
				add(m, info)
				continue
			}
			base := strings.Count(filepath.Clean(m), string(filepath.Separator))
			_ = filepath.WalkDir(m, func(q string, d fs.DirEntry, err error) error {
				if err != nil {
					return nil
				}
				if d.IsDir() {
					if strings.Count(filepath.Clean(q), string(filepath.Separator))-base >= 4 {
						return filepath.SkipDir
					}
					return nil
				}
				if i, err := d.Info(); err == nil {
					add(q, i)
				}
				return nil
			})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].mod.After(out[j].mod) })
	return out, errs
}

// Search runs the query.
func Search(ctx context.Context, q Query) (*Result, error) {
	if strings.TrimSpace(q.Pattern) == "" || len(q.Pattern) > 500 {
		return nil, errors.New("give a pattern of at most 500 characters")
	}
	expr := q.Pattern
	if q.IgnoreCase == nil || *q.IgnoreCase {
		expr = "(?i)" + expr
	}
	re, err := regexp.Compile(expr)
	if err != nil {
		return nil, errors.New("invalid pattern: " + err.Error())
	}
	if q.MaxMatches <= 0 {
		q.MaxMatches = 200
	}
	q.MaxMatches = min(q.MaxMatches, 5000)
	paths := q.Paths
	if len(paths) == 0 {
		paths = DefaultPaths()
	}
	for _, p := range paths {
		if !filepath.IsAbs(p) {
			return nil, errors.New("paths must be absolute: " + p)
		}
	}
	var since time.Time
	if q.ModifiedWithin > 0 {
		since = time.Now().Add(-time.Duration(q.ModifiedWithin) * time.Hour)
	}
	files, errs := collect(paths, since, q.Gzip)
	res := &Result{Matches: []Match{}, Errors: errs}
	deadline := time.Now().Add(MaxDuration)
	for _, f := range files {
		if ctx.Err() != nil {
			res.Stopped = "cancelled"
			break
		}
		if time.Now().After(deadline) {
			res.Stopped = "time limit reached"
			break
		}
		if res.BytesScanned > MaxBytes {
			res.Stopped = "size limit reached"
			break
		}
		if err := scanFile(f.path, re, q.MaxMatches, res); err != nil {
			res.Errors = append(res.Errors, f.path+": "+err.Error())
		}
		res.FilesScanned++
		if res.Truncated {
			break
		}
	}
	if len(res.Errors) > 20 {
		res.Errors = res.Errors[:20]
	}
	return res, nil
}

func scanFile(path string, re *regexp.Regexp, max int, res *Result) error {
	fh, err := os.Open(path)
	if err != nil {
		return err
	}
	defer fh.Close()
	var r io.Reader = fh
	if strings.HasSuffix(strings.ToLower(path), ".gz") {
		gz, err := gzip.NewReader(fh)
		if err != nil {
			return err
		}
		defer gz.Close()
		r = gz
	}
	br := bufio.NewReaderSize(r, 64*1024)
	head, _ := br.Peek(8192)
	if bytes.IndexByte(head, 0) >= 0 {
		return nil // binary
	}
	sc := bufio.NewScanner(br)
	sc.Buffer(make([]byte, 64*1024), 4*1024*1024)
	n := 0
	for sc.Scan() {
		n++
		b := sc.Bytes()
		res.BytesScanned += int64(len(b) + 1)
		if !re.Match(b) {
			continue
		}
		text := strings.TrimRight(string(b), "\r")
		if len(text) > 2000 {
			text = text[:2000] + "…"
		}
		res.Matches = append(res.Matches, Match{File: path, Line: n, Text: text})
		if len(res.Matches) >= max {
			res.Truncated = true
			return nil
		}
	}
	return sc.Err()
}
