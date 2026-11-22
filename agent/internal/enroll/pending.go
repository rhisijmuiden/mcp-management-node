package enroll

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"

	"github.com/rhisijmuiden/mcp-management-node/agent/internal/config"
)

// Pending is a pairing that an installer left for the service to finish: the Windows MSI writes
// it to the registry (SERVER, FINGERPRINT and CODE properties), any installer can also write
// enroll.json next to the config.
type Pending struct {
	Server      string `json:"server"`
	Fingerprint string `json:"fingerprint"`
	Code        string `json:"code"`
	Name        string `json:"name"`
}

func pendingFile() string { return filepath.Join(config.Dir, "enroll.json") }

// LoadPending returns a pending pairing, if there is one, and a function that removes it.
func LoadPending() (*Pending, func()) {
	if b, err := os.ReadFile(pendingFile()); err == nil {
		var p Pending
		if json.Unmarshal(b, &p) == nil && p.complete() {
			return &p, func() { os.Remove(pendingFile()) }
		}
	}
	if p := loadPlatformPending(); p != nil && p.complete() {
		return p, clearPlatformPending
	}
	return nil, func() {}
}

func (p *Pending) complete() bool {
	return strings.TrimSpace(p.Server) != "" && strings.TrimSpace(p.Fingerprint) != "" && strings.TrimSpace(p.Code) != ""
}
