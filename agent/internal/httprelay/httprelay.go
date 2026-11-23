// Package httprelay makes an HTTPS request on behalf of the app, for APIs that are only reachable inside the
// customer's network (a UniFi console behind NAT). The app sends the full request; the agent returns the status,
// a few headers, the body (base64) and the certificate the server presented. A pinned SHA-256 fingerprint is
// checked during the TLS handshake, before any header (such as an API key) is sent.
package httprelay

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// MaxBody is the largest response body returned to the app.
const MaxBody = 16 << 20

type Request struct {
	Method      string            `json:"method"`
	URL         string            `json:"url"`
	Headers     map[string]string `json:"headers"`
	Body        string            `json:"body"` // base64
	Timeout     int               `json:"timeout"`
	Insecure    bool              `json:"insecure"`
	Fingerprint string            `json:"fingerprint"` // sha256 hex of the server certificate (DER)
}

type Certificate struct {
	SHA256     string `json:"sha256"`
	Subject    string `json:"subject"`
	Issuer     string `json:"issuer"`
	NotAfter   string `json:"not_after"`
	SelfSigned bool   `json:"self_signed"`
}

type Response struct {
	Status          int               `json:"status"`
	Headers         map[string]string `json:"headers"`
	Body            string            `json:"body"`
	BodyB64         bool              `json:"body_b64"`
	PeerCertificate *Certificate      `json:"peer_certificate,omitempty"`
}

var allowedMethods = map[string]bool{"GET": true, "HEAD": true, "POST": true, "PUT": true, "PATCH": true, "DELETE": true}

func certInfo(c *x509.Certificate) *Certificate {
	sum := sha256.Sum256(c.Raw)
	return &Certificate{SHA256: hex.EncodeToString(sum[:]), Subject: c.Subject.String(), Issuer: c.Issuer.String(),
		NotAfter: c.NotAfter.UTC().Format(time.RFC3339), SelfSigned: c.Subject.String() == c.Issuer.String()}
}

// Normalize returns the lower case hex form of a SHA-256 fingerprint ("AB:CD:..." or "abcd...").
func Normalize(fp string) (string, error) {
	h := strings.ToLower(strings.NewReplacer(":", "", " ", "").Replace(strings.TrimSpace(fp)))
	if len(h) != 64 {
		return "", errors.New("fingerprint must be a SHA-256 hash")
	}
	if _, err := hex.DecodeString(h); err != nil {
		return "", errors.New("fingerprint must be hexadecimal")
	}
	return h, nil
}

// Do runs one request.
func Do(ctx context.Context, q Request) (*Response, error) {
	method := strings.ToUpper(q.Method)
	if method == "" {
		method = "GET"
	}
	if !allowedMethods[method] {
		return nil, fmt.Errorf("method %s not allowed", method)
	}
	u, err := url.Parse(q.URL)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" {
		return nil, errors.New("url must be an http(s) address")
	}
	want := ""
	if q.Fingerprint != "" {
		if want, err = Normalize(q.Fingerprint); err != nil {
			return nil, err
		}
	}
	timeout := time.Duration(q.Timeout) * time.Second
	if timeout <= 0 || timeout > 120*time.Second {
		timeout = 30 * time.Second
	}
	var peer *Certificate
	tlsConf := &tls.Config{MinVersion: tls.VersionTLS12, InsecureSkipVerify: q.Insecure || want != ""} //nolint:gosec // verified below when pinned
	tlsConf.VerifyConnection = func(cs tls.ConnectionState) error {
		if len(cs.PeerCertificates) == 0 {
			return errors.New("server sent no certificate")
		}
		peer = certInfo(cs.PeerCertificates[0])
		if want != "" && peer.SHA256 != want {
			return fmt.Errorf("certificate fingerprint %s does not match the pinned fingerprint", peer.SHA256)
		}
		return nil
	}
	tr := &http.Transport{TLSClientConfig: tlsConf, Proxy: nil, ResponseHeaderTimeout: timeout, DisableKeepAlives: true}
	hc := &http.Client{Transport: tr, Timeout: timeout,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	var body io.Reader
	if q.Body != "" {
		b, err := base64.StdEncoding.DecodeString(q.Body)
		if err != nil {
			return nil, errors.New("body must be base64")
		}
		body = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, u.String(), body)
	if err != nil {
		return nil, err
	}
	for k, v := range q.Headers {
		req.Header.Set(k, v)
	}
	resp, err := hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, MaxBody+1))
	if err != nil {
		return nil, err
	}
	if len(data) > MaxBody {
		return nil, errors.New("response too large")
	}
	out := &Response{Status: resp.StatusCode, Headers: map[string]string{}, Body: base64.StdEncoding.EncodeToString(data),
		BodyB64: true, PeerCertificate: peer}
	for _, h := range []string{"Content-Type", "Retry-After", "Location", "Mcp-Session-Id", "Www-Authenticate"} {
		if v := resp.Header.Get(h); v != "" {
			out.Headers[h] = v
		}
	}
	return out, nil
}
