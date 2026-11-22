package tlsutil

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"errors"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func selfSignedCA(t *testing.T, cn string) *x509.Certificate {
	t.Helper()
	k, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tpl := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: cn}, IsCA: true,
		BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour)}
	der, err := x509.CreateCertificate(rand.Reader, tpl, tpl, &k.PublicKey, k)
	if err != nil {
		t.Fatal(err)
	}
	c, _ := x509.ParseCertificate(der)
	return c
}

// A device that is not the app (here: httptest's own certificate) must give a WrongServerError
// that survives the HTTP client's wrapping, so the agent log can explain it.
func TestWrongServerIsExplained(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	defer srv.Close()
	ca := selfSignedCA(t, "MCP Management Node agent CA")
	server := strings.TrimPrefix(srv.URL, "https://")

	hc := &http.Client{Transport: &http.Transport{TLSClientConfig: ClientConfig(tls.Certificate{}, ca)}}
	_, err := hc.Get(srv.URL)
	var w *WrongServerError
	if !errors.As(err, &w) {
		t.Fatalf("want WrongServerError, got %v", err)
	}
	msg := Explain(server, err).Error()
	if !strings.Contains(msg, "Is SERVER ("+server+") pointing at the app?") || !strings.Contains(msg, "subject") {
		t.Fatalf("unexpected message: %s", msg)
	}

	var got *x509.Certificate
	hc = &http.Client{Transport: &http.Transport{TLSClientConfig: PinnedEnrollConfig(Fingerprint(ca.Raw), &got)}}
	_, err = hc.Get(srv.URL)
	if !errors.As(err, &w) || got != nil {
		t.Fatalf("enroll: want WrongServerError, got %v", err)
	}
	if e := errors.New("connection refused"); Explain(server, e) != e {
		t.Fatal("other errors must pass through unchanged")
	}
}
