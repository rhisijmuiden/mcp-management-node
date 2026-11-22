// Package transfer copies files and folders between two servers through the app.
//
// The source agent packs the paths into a gzip-compressed tar stream and uploads it to the app
// (PUT, over the agent's mutual-TLS connection); the target agent downloads that stream and
// unpacks it below the target folder. Nothing is opened between the two servers themselves.
package transfer

import (
	"archive/tar"
	"compress/gzip"
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"time"
)

type Result struct {
	Files int   `json:"files"`
	Dirs  int   `json:"dirs"`
	Bytes int64 `json:"bytes"` // uncompressed
	Wire  int64 `json:"wire_bytes,omitempty"`
}

type progress struct {
	n    atomic.Int64
	last time.Time
	line func(string)
	verb string
}

func (p *progress) add(n int64) {
	v := p.n.Add(n)
	if time.Since(p.last) > 5*time.Second {
		p.last = time.Now()
		p.line(fmt.Sprintf("%s %s", p.verb, human(v)))
	}
}

func human(n int64) string {
	const unit = 1024
	if n < unit {
		return fmt.Sprintf("%d B", n)
	}
	div, exp := int64(unit), 0
	for m := n / unit; m >= unit; m /= unit {
		div *= unit
		exp++
	}
	return fmt.Sprintf("%.1f %ciB", float64(n)/float64(div), "KMGTPE"[exp])
}

type countingWriter struct {
	w io.Writer
	n int64
}

func (c *countingWriter) Write(b []byte) (int, error) {
	n, err := c.w.Write(b)
	c.n += int64(n)
	return n, err
}

// Pack writes paths as tar.gz to w. Each path becomes a top-level entry named after its base name.
func Pack(ctx context.Context, w io.Writer, paths []string, line func(string)) (Result, error) {
	var res Result
	gz, _ := gzip.NewWriterLevel(w, gzip.BestSpeed)
	tw := tar.NewWriter(gz)
	pr := &progress{line: line, verb: "sent", last: time.Now()}
	for _, root := range paths {
		if !filepath.IsAbs(root) {
			return res, errors.New("source paths must be absolute: " + root)
		}
		root = filepath.Clean(root)
		info, err := os.Lstat(root)
		if err != nil {
			return res, err
		}
		base := filepath.Base(root)
		if base == string(filepath.Separator) || base == "." || (runtime.GOOS == "windows" && strings.HasSuffix(base, ":\\")) {
			return res, errors.New("copying a whole drive or / is not supported: name a folder")
		}
		line(fmt.Sprintf("packing %s", root))
		err = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
			if err != nil {
				line("skipped " + p + ": " + err.Error())
				return nil
			}
			if ctx.Err() != nil {
				return ctx.Err()
			}
			fi, err := d.Info()
			if err != nil {
				return nil
			}
			rel, _ := filepath.Rel(root, p)
			name := filepath.ToSlash(filepath.Join(base, rel))
			link := ""
			if fi.Mode()&fs.ModeSymlink != 0 {
				link, _ = os.Readlink(p)
			} else if !fi.Mode().IsRegular() && !fi.IsDir() {
				return nil // sockets, devices, pipes
			}
			hdr, err := tar.FileInfoHeader(fi, link)
			if err != nil {
				return nil
			}
			hdr.Name = name
			if fi.IsDir() {
				hdr.Name += "/"
				res.Dirs++
			}
			fillOwner(hdr, fi)
			if err := tw.WriteHeader(hdr); err != nil {
				return err
			}
			if !fi.Mode().IsRegular() {
				return nil
			}
			f, err := os.Open(p)
			if err != nil {
				line("skipped " + p + ": " + err.Error())
				return nil
			}
			n, err := io.Copy(tw, io.TeeReader(f, writerFunc(func(b []byte) { pr.add(int64(len(b))) })))
			f.Close()
			if err != nil {
				return fmt.Errorf("%s: %w", p, err)
			}
			res.Files++
			res.Bytes += n
			return nil
		})
		if err != nil {
			return res, err
		}
		_ = info
	}
	if err := tw.Close(); err != nil {
		return res, err
	}
	return res, gz.Close()
}

type writerFunc func([]byte)

func (f writerFunc) Write(b []byte) (int, error) { f(b); return len(b), nil }

// Send packs paths and uploads the stream to url.
func Send(ctx context.Context, hc *http.Client, url string, paths []string, line func(string)) (Result, error) {
	pr, pw := io.Pipe()
	cw := &countingWriter{w: pw}
	var res Result
	var packErr error
	go func() {
		res, packErr = Pack(ctx, cw, paths, line)
		pw.CloseWithError(packErr)
	}()
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, url, pr)
	if err != nil {
		pr.Close()
		return res, err
	}
	req.Header.Set("Content-Type", "application/gzip")
	resp, err := hc.Do(req)
	if err != nil {
		pr.CloseWithError(err)
		if packErr != nil {
			return res, packErr
		}
		return res, fmt.Errorf("upload: %w", err)
	}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	resp.Body.Close()
	if packErr != nil {
		return res, packErr
	}
	if resp.StatusCode/100 != 2 {
		return res, fmt.Errorf("upload refused: HTTP %d %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	res.Wire = cw.n
	line(fmt.Sprintf("uploaded %d files, %s (%s compressed)", res.Files, human(res.Bytes), human(res.Wire)))
	return res, nil
}

// Receive downloads the stream from url and unpacks it below dest.
func Receive(ctx context.Context, hc *http.Client, url, dest string, overwrite bool, line func(string)) (Result, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return Result{}, err
	}
	resp, err := hc.Do(req)
	if err != nil {
		return Result{}, fmt.Errorf("download: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return Result{}, fmt.Errorf("download refused: HTTP %d", resp.StatusCode)
	}
	return Unpack(ctx, resp.Body, dest, overwrite, line)
}

// Unpack extracts a tar.gz stream below dest, refusing entries that would end up outside it.
func Unpack(ctx context.Context, r io.Reader, dest string, overwrite bool, line func(string)) (Result, error) {
	var res Result
	if !filepath.IsAbs(dest) {
		return res, errors.New("the target path must be absolute")
	}
	dest = filepath.Clean(dest)
	if err := os.MkdirAll(dest, 0o755); err != nil {
		return res, err
	}
	gz, err := gzip.NewReader(r)
	if err != nil {
		return res, err
	}
	tr := tar.NewReader(gz)
	pr := &progress{line: line, verb: "received", last: time.Now()}
	type dirTime struct {
		path string
		t    time.Time
	}
	var dirs []dirTime
	for {
		if ctx.Err() != nil {
			return res, ctx.Err()
		}
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return res, err
		}
		name := filepath.FromSlash(hdr.Name)
		if filepath.IsAbs(name) || strings.HasPrefix(filepath.Clean(name), "..") || strings.Contains(name, ":") && runtime.GOOS == "windows" {
			return res, fmt.Errorf("refusing unsafe path %q", hdr.Name)
		}
		target := filepath.Join(dest, name)
		if target != dest && !strings.HasPrefix(target, dest+string(filepath.Separator)) {
			return res, fmt.Errorf("refusing unsafe path %q", hdr.Name)
		}
		if err := noSymlinkParents(dest, target); err != nil {
			return res, err
		}
		mode := hdr.FileInfo().Mode()
		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, mode.Perm()|0o700); err != nil {
				return res, err
			}
			setOwner(target, hdr)
			dirs = append(dirs, dirTime{target, hdr.ModTime})
			res.Dirs++
		case tar.TypeReg:
			if _, err := os.Lstat(target); err == nil && !overwrite {
				return res, fmt.Errorf("%s exists already (copy again with overwrite to replace files)", target)
			}
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return res, err
			}
			tmp := target + ".mmn-part"
			f, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, mode.Perm())
			if err != nil {
				return res, err
			}
			n, err := io.Copy(f, io.TeeReader(tr, writerFunc(func(b []byte) { pr.add(int64(len(b))) })))
			if cerr := f.Close(); err == nil {
				err = cerr
			}
			if err != nil {
				os.Remove(tmp)
				return res, err
			}
			if runtime.GOOS == "windows" {
				os.Remove(target)
			}
			if err := os.Rename(tmp, target); err != nil {
				os.Remove(tmp)
				return res, err
			}
			_ = os.Chmod(target, mode.Perm())
			setOwner(target, hdr)
			_ = os.Chtimes(target, hdr.ModTime, hdr.ModTime)
			res.Files++
			res.Bytes += n
		case tar.TypeSymlink:
			if runtime.GOOS == "windows" {
				line("skipped symlink " + hdr.Name)
				continue
			}
			// Only links that stay inside the target folder.
			resolved := hdr.Linkname
			if !filepath.IsAbs(resolved) {
				resolved = filepath.Join(filepath.Dir(target), resolved)
			}
			if resolved = filepath.Clean(resolved); resolved != dest && !strings.HasPrefix(resolved, dest+string(filepath.Separator)) {
				line("skipped symlink pointing outside the target: " + hdr.Name + " -> " + hdr.Linkname)
				continue
			}
			if _, err := os.Lstat(target); err == nil {
				if !overwrite {
					return res, fmt.Errorf("%s exists already (copy again with overwrite to replace files)", target)
				}
				os.Remove(target)
			}
			if err := os.Symlink(hdr.Linkname, target); err != nil {
				return res, err
			}
		}
	}
	for i := len(dirs) - 1; i >= 0; i-- {
		_ = os.Chtimes(dirs[i].path, dirs[i].t, dirs[i].t)
	}
	line(fmt.Sprintf("unpacked %d files in %d folders, %s, into %s", res.Files, res.Dirs, human(res.Bytes), dest))
	return res, nil
}

// noSymlinkParents refuses to write through a symlinked folder below dest.
func noSymlinkParents(dest, target string) error {
	rel, err := filepath.Rel(dest, filepath.Dir(target))
	if err != nil || rel == "." {
		return nil
	}
	cur := dest
	for _, part := range strings.Split(rel, string(filepath.Separator)) {
		cur = filepath.Join(cur, part)
		if fi, err := os.Lstat(cur); err == nil && fi.Mode()&fs.ModeSymlink != 0 {
			return fmt.Errorf("refusing to write through the symlink %s", cur)
		}
	}
	return nil
}
