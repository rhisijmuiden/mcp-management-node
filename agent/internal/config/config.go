// Package config holds the agent's on-disk configuration and state.
//
// Linux:   /etc/mmn-agent/{config.json,agent.key,agent.crt,ca.crt}, state in /var/lib/mmn-agent/state.json
// Windows: %ProgramData%\mmn-agent\ for both.
package config

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"sync"
)

// Config is written once at enrollment.
type Config struct {
	// Server is the agent endpoint of Stackpilot, host:port (mTLS).
	Server string `json:"server"`
	NodeID string `json:"node_id"`
	// CAFingerprint is the SHA-256 of the app's agent CA certificate (DER), hex.
	CAFingerprint string `json:"ca_fingerprint"`
}

// State is changed at runtime by the app (desired CPU governor, ...).
type State struct {
	Governor string `json:"governor,omitempty"`
}

var (
	// Dir can be overridden for tests or with MMN_AGENT_DIR.
	Dir      = defaultDir()
	StateDir = defaultStateDir()
	mu       sync.Mutex
)

func defaultDir() string {
	if d := os.Getenv("MMN_AGENT_DIR"); d != "" {
		return d
	}
	if runtime.GOOS == "windows" {
		return filepath.Join(programData(), "mmn-agent")
	}
	return "/etc/mmn-agent"
}

func defaultStateDir() string {
	if d := os.Getenv("MMN_AGENT_STATE_DIR"); d != "" {
		return d
	}
	if d := os.Getenv("MMN_AGENT_DIR"); d != "" {
		return d
	}
	if runtime.GOOS == "windows" {
		return filepath.Join(programData(), "mmn-agent")
	}
	return "/var/lib/mmn-agent"
}

func programData() string {
	if p := os.Getenv("ProgramData"); p != "" {
		return p
	}
	return `C:\ProgramData`
}

func ConfigFile() string { return filepath.Join(Dir, "config.json") }
func KeyFile() string    { return filepath.Join(Dir, "agent.key") }
func CertFile() string   { return filepath.Join(Dir, "agent.crt") }
func CAFile() string     { return filepath.Join(Dir, "ca.crt") }
func StateFile() string  { return filepath.Join(StateDir, "state.json") }

var ErrNotEnrolled = errors.New("this agent is not enrolled yet: run 'mmn-agent enroll --code <code> ...' first")

func Load() (*Config, error) {
	b, err := os.ReadFile(ConfigFile())
	if errors.Is(err, os.ErrNotExist) {
		return nil, ErrNotEnrolled
	}
	if err != nil {
		return nil, err
	}
	var c Config
	if err := json.Unmarshal(b, &c); err != nil {
		return nil, err
	}
	if c.Server == "" || c.NodeID == "" {
		return nil, ErrNotEnrolled
	}
	return &c, nil
}

// WriteFileAtomic writes data with the given mode via a temp file and rename.
func WriteFileAtomic(path string, data []byte, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".tmp-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if err := tmp.Chmod(mode); err != nil && runtime.GOOS != "windows" {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}

func Save(c *Config) error {
	b, _ := json.MarshalIndent(c, "", "  ")
	return WriteFileAtomic(ConfigFile(), b, 0o600)
}

func LoadState() State {
	mu.Lock()
	defer mu.Unlock()
	var s State
	if b, err := os.ReadFile(StateFile()); err == nil {
		_ = json.Unmarshal(b, &s)
	}
	return s
}

func UpdateState(fn func(*State)) error {
	mu.Lock()
	defer mu.Unlock()
	var s State
	if b, err := os.ReadFile(StateFile()); err == nil {
		_ = json.Unmarshal(b, &s)
	}
	fn(&s)
	b, _ := json.MarshalIndent(s, "", "  ")
	return WriteFileAtomic(StateFile(), b, 0o600)
}

// Remove deletes the enrollment (keys, certificates, config).
func Remove() error {
	var first error
	for _, f := range []string{ConfigFile(), KeyFile(), CertFile(), CAFile()} {
		if err := os.Remove(f); err != nil && !errors.Is(err, os.ErrNotExist) && first == nil {
			first = err
		}
	}
	return first
}
