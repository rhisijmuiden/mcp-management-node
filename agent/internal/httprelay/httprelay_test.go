package httprelay

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestPinnedRequest(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-API-KEY") != "k" {
			w.WriteHeader(401)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"applicationVersion":"10.1.84"}`))
	}))
	defer srv.Close()
	sum := sha256.Sum256(srv.Certificate().Raw)
	fp := hex.EncodeToString(sum[:])

	res, err := Do(context.Background(), Request{URL: srv.URL + "/v1/info", Headers: map[string]string{"X-API-KEY": "k"}, Fingerprint: strings.ToUpper(fp)})
	if err != nil {
		t.Fatal(err)
	}
	body, _ := base64.StdEncoding.DecodeString(res.Body)
	if res.Status != 200 || !strings.Contains(string(body), "10.1.84") || res.PeerCertificate.SHA256 != fp {
		t.Fatalf("unexpected %+v %s", res, body)
	}

	// Wrong pin: the handshake fails, so the key is never sent.
	_, err = Do(context.Background(), Request{URL: srv.URL + "/v1/info", Fingerprint: strings.Repeat("ab", 32)})
	if err == nil || !strings.Contains(err.Error(), "fingerprint") {
		t.Fatalf("want fingerprint error, got %v", err)
	}
	// Not pinned and not insecure: the self-signed test certificate is refused.
	if _, err = Do(context.Background(), Request{URL: srv.URL}); err == nil {
		t.Fatal("want verification error")
	}
	// Insecure returns the certificate, to pin it.
	res, err = Do(context.Background(), Request{Method: "HEAD", URL: srv.URL, Insecure: true})
	if err != nil || res.PeerCertificate == nil || res.PeerCertificate.SHA256 != fp {
		t.Fatalf("insecure: %v %+v", err, res)
	}
}

func TestValidation(t *testing.T) {
	if _, err := Do(context.Background(), Request{URL: "file:///etc/passwd"}); err == nil {
		t.Fatal("file URL accepted")
	}
	if _, err := Do(context.Background(), Request{Method: "CONNECT", URL: "https://x"}); err == nil {
		t.Fatal("CONNECT accepted")
	}
	if _, err := Normalize("zz"); err == nil {
		t.Fatal("bad fingerprint accepted")
	}
}
