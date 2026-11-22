// Package tlsutil builds the TLS configuration used to reach the app's agent endpoint.
//
// The app runs its own private CA for agents. The agent trusts only that CA (pinned by its
// SHA-256 fingerprint at enrollment) and does not check the host name: the endpoint can be
// reached by IP, LAN name or a public name through a TCP proxy without re-issuing certificates.
package tlsutil

import (
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
)

// Fingerprint returns the lowercase hex SHA-256 of a DER certificate.
func Fingerprint(der []byte) string {
	sum := sha256.Sum256(der)
	return hex.EncodeToString(sum[:])
}

// NormalizeFingerprint accepts "sha256:AB:CD..." / "abcd..." and returns lowercase hex.
func NormalizeFingerprint(fp string) string {
	fp = strings.TrimSpace(strings.ToLower(fp))
	fp = strings.TrimPrefix(fp, "sha256:")
	fp = strings.ReplaceAll(fp, ":", "")
	return fp
}

// verifyChain checks that the server's leaf certificate was issued by ca.
func verifyChain(certs []*x509.Certificate, ca *x509.Certificate) error {
	if len(certs) == 0 {
		return errors.New("server sent no certificate")
	}
	pool := x509.NewCertPool()
	pool.AddCert(ca)
	inter := x509.NewCertPool()
	for _, c := range certs[1:] {
		inter.AddCert(c)
	}
	_, err := certs[0].Verify(x509.VerifyOptions{Roots: pool, Intermediates: inter,
		KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}})
	return err
}

// PinnedEnrollConfig is used before the agent has the CA: the server must present a chain
// containing a CA certificate whose fingerprint equals pin, and the leaf must be signed by it.
// The verified CA is stored in *got.
func PinnedEnrollConfig(pin string, got **x509.Certificate) *tls.Config {
	pin = NormalizeFingerprint(pin)
	return &tls.Config{
		MinVersion:         tls.VersionTLS12,
		InsecureSkipVerify: true, // replaced by VerifyConnection below
		VerifyConnection: func(cs tls.ConnectionState) error {
			for _, c := range cs.PeerCertificates {
				if c.IsCA && Fingerprint(c.Raw) == pin {
					if err := verifyChain(cs.PeerCertificates, c); err != nil {
						return fmt.Errorf("server certificate not signed by the pinned CA: %w", err)
					}
					*got = c
					return nil
				}
			}
			return fmt.Errorf("the server's CA does not match fingerprint %s (wrong server, or a man in the middle)", pin)
		},
	}
}

// ClientConfig is used for the mTLS connection after enrollment.
func ClientConfig(cert tls.Certificate, ca *x509.Certificate) *tls.Config {
	return &tls.Config{
		MinVersion:         tls.VersionTLS12,
		Certificates:       []tls.Certificate{cert},
		InsecureSkipVerify: true, // replaced by VerifyConnection below
		VerifyConnection: func(cs tls.ConnectionState) error {
			return verifyChain(cs.PeerCertificates, ca)
		},
	}
}
