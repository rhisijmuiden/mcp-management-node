// Package enroll pairs this machine with MCP Management Node using a one-time code.
//
// The agent creates its own private key (it never leaves the machine), sends a certificate
// request with the code to the app's agent endpoint, and receives a client certificate signed
// by the app's agent CA. The CA is pinned by the fingerprint shown in the app.
package enroll

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"runtime"
	"strings"
	"time"

	"github.com/rhisijmuiden/mcp-management-node/agent/internal/config"
	"github.com/rhisijmuiden/mcp-management-node/agent/internal/tlsutil"
)

type Request struct {
	Code         string `json:"code"`
	CSR          string `json:"csr"`
	Hostname     string `json:"hostname"`
	OS           string `json:"os"`
	Arch         string `json:"arch"`
	AgentVersion string `json:"agent_version"`
	Kind         string `json:"kind"` // proxmox | linux | windows
}

type Response struct {
	NodeID      string `json:"node_id"`
	Certificate string `json:"certificate"`
	CA          string `json:"ca"`
	Server      string `json:"server"`
	Environment string `json:"environment"`
	NodeName    string `json:"node_name"`
}

// NormalizeServer turns "host", "host:port" or "https://host:port/..." into host:port.
func NormalizeServer(s string) string {
	s = strings.TrimSpace(s)
	s = strings.TrimPrefix(strings.TrimPrefix(s, "https://"), "wss://")
	if i := strings.IndexByte(s, '/'); i >= 0 {
		s = s[:i]
	}
	if !strings.Contains(s, ":") || (strings.HasPrefix(s, "[") && strings.HasSuffix(s, "]")) {
		s += ":8443"
	}
	return s
}

func Kind() string {
	if runtime.GOOS == "windows" {
		return "windows"
	}
	if _, err := os.Stat("/usr/bin/pveversion"); err == nil {
		return "proxmox"
	}
	return "linux"
}

// Run enrolls and writes config, key and certificates. hostname overrides the reported name.
func Run(server, fingerprint, code, hostname, version string) (*Response, error) {
	if code == "" || fingerprint == "" || server == "" {
		return nil, errors.New("--server, --fingerprint and --code are required")
	}
	server = NormalizeServer(server)
	if hostname == "" {
		hostname, _ = os.Hostname()
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	csrDER, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{
		Subject: pkix.Name{CommonName: hostname, Organization: []string{"mmn-agent"}},
	}, key)
	if err != nil {
		return nil, err
	}
	body, _ := json.Marshal(Request{
		Code: strings.TrimSpace(code), CSR: string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: csrDER})),
		Hostname: hostname, OS: runtime.GOOS, Arch: runtime.GOARCH, AgentVersion: version, Kind: Kind(),
	})
	var ca *x509.Certificate
	hc := &http.Client{Timeout: 30 * time.Second, Transport: &http.Transport{
		TLSClientConfig: tlsutil.PinnedEnrollConfig(fingerprint, &ca), Proxy: http.ProxyFromEnvironment}}
	resp, err := hc.Post("https://"+server+"/agent/enroll", "application/json", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("could not reach %s: %w", server, tlsutil.Explain(server, err))
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK {
		var e struct {
			Detail any `json:"detail"`
		}
		_ = json.Unmarshal(raw, &e)
		if e.Detail != nil {
			return nil, fmt.Errorf("enrollment refused: %v", e.Detail)
		}
		return nil, fmt.Errorf("enrollment refused: HTTP %d", resp.StatusCode)
	}
	var out Response
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("bad answer from server: %w", err)
	}
	// The CA in the answer must be the one we pinned.
	blk, _ := pem.Decode([]byte(out.CA))
	if blk == nil || tlsutil.Fingerprint(blk.Bytes) != tlsutil.NormalizeFingerprint(fingerprint) {
		return nil, errors.New("the CA in the answer does not match the pinned fingerprint")
	}
	cblk, _ := pem.Decode([]byte(out.Certificate))
	if cblk == nil {
		return nil, errors.New("no certificate in the answer")
	}
	cert, err := x509.ParseCertificate(cblk.Bytes)
	if err != nil {
		return nil, err
	}
	if _, err := cert.Verify(x509.VerifyOptions{Roots: poolOf(ca), KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}}); err != nil {
		return nil, fmt.Errorf("issued certificate does not verify: %w", err)
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return nil, err
	}
	if out.Server == "" {
		out.Server = server
	}
	if err := config.WriteFileAtomic(config.KeyFile(), pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}), 0o600); err != nil {
		return nil, err
	}
	if err := config.WriteFileAtomic(config.CertFile(), []byte(out.Certificate), 0o600); err != nil {
		return nil, err
	}
	if err := config.WriteFileAtomic(config.CAFile(), []byte(out.CA), 0o644); err != nil {
		return nil, err
	}
	if err := config.Save(&config.Config{Server: NormalizeServer(out.Server), NodeID: out.NodeID,
		CAFingerprint: tlsutil.NormalizeFingerprint(fingerprint)}); err != nil {
		return nil, err
	}
	return &out, nil
}

func poolOf(c *x509.Certificate) *x509.CertPool {
	p := x509.NewCertPool()
	p.AddCert(c)
	return p
}
