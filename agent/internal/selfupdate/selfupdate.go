// Package selfupdate replaces the agent with a newer build handed out by the app.
//
// Debian package installs (.deb) are upgraded with dpkg in a separate systemd unit, so the
// upgrade is not killed when the agent service restarts. Plain binary installs (generic Linux,
// Windows) swap the executable and exit; the service manager starts the new version.
package selfupdate

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/rhisijmuiden/mcp-management-node/agent/internal/config"
)

type Request struct {
	URL     string `json:"url"`
	SHA256  string `json:"sha256"`
	Version string `json:"version"`
	Format  string `json:"format"` // deb | binary
}

// InstallKind reports how this agent was installed.
func InstallKind() string {
	if runtime.GOOS == "windows" {
		return "windows"
	}
	if exe, err := os.Executable(); err == nil && exe == "/usr/bin/mmn-agent" {
		if _, err := os.Stat("/var/lib/dpkg/info/mmn-agent.list"); err == nil {
			return "deb"
		}
	}
	return "binary"
}

// Download fetches the file with the agent's mTLS client and checks the SHA-256.
func Download(hc *http.Client, req Request) (string, error) {
	if req.URL == "" || len(req.SHA256) != 64 {
		return "", errors.New("url and sha256 are required")
	}
	dir := filepath.Join(config.StateDir, "update")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	name := "mmn-agent.download"
	if req.Format == "deb" {
		name = "mmn-agent.deb"
	}
	dst := filepath.Join(dir, name)
	resp, err := hc.Get(req.URL)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("download failed: HTTP %d", resp.StatusCode)
	}
	f, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o700)
	if err != nil {
		return "", err
	}
	h := sha256.New()
	_, err = io.Copy(io.MultiWriter(f, h), io.LimitReader(resp.Body, 200<<20))
	f.Close()
	if err != nil {
		return "", err
	}
	if got := hex.EncodeToString(h.Sum(nil)); got != strings.ToLower(req.SHA256) {
		os.Remove(dst)
		return "", fmt.Errorf("checksum mismatch (got %s)", got)
	}
	return dst, nil
}

// Install starts the upgrade. For "binary" it returns restart=true: the caller exits soon after.
func Install(file, format string) (restart bool, err error) {
	if format == "deb" {
		unit := fmt.Sprintf("mmn-agent-update-%d", time.Now().Unix())
		cmd := exec.Command("systemd-run", "--unit", unit, "--collect", "--no-block", "--quiet",
			"--setenv=DEBIAN_FRONTEND=noninteractive", "dpkg", "-i", file)
		if out, err := cmd.CombinedOutput(); err != nil {
			return false, fmt.Errorf("systemd-run dpkg: %v: %s", err, strings.TrimSpace(string(out)))
		}
		return false, nil
	}
	exe, err := os.Executable()
	if err != nil {
		return false, err
	}
	if runtime.GOOS == "windows" {
		old := exe + ".old"
		os.Remove(old)
		if err := os.Rename(exe, old); err != nil {
			return false, err
		}
	}
	tmp := exe + ".new"
	if err := copyFile(file, tmp); err != nil {
		return false, err
	}
	if err := os.Rename(tmp, exe); err != nil {
		return false, err
	}
	return true, nil
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}
