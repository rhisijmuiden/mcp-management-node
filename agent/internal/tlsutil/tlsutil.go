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

// WrongServerError means the address answered with TLS, but with a certificate that was not
// issued by the app's agent CA: SERVER points at another device or at a TLS-terminating proxy.
type WrongServerError struct {
	Presented string // subject and issuer of the certificate the server sent
	Err       error
}

func (e *WrongServerError) Error() string {
	return fmt.Sprintf("the server's certificate (%s) is not from this app's agent CA: %v", e.Presented, e.Err)
}

func (e *WrongServerError) Unwrap() error { return e.Err }

func describe(certs []*x509.Certificate) string {
	if len(certs) == 0 {
		return "no certificate"
	}
	c := certs[0]
	d := "subject " + c.Subject.String()
	if c.Issuer.String() != c.Subject.String() {
		d += ", issuer " + c.Issuer.String()
	}
	if len(c.DNSNames) > 0 {
		d += ", names " + strings.Join(c.DNSNames, " ")
	}
	return d
}

// Explain adds a hint for the agent log when err means the agent is talking to the wrong server.
func Explain(server string, err error) error {
	var w *WrongServerError
	if errors.As(err, &w) {
		return fmt.Errorf("%w. Is SERVER (%s) pointing at the app? It must reach the app's agent port directly "+
			"(the app's LAN IP) or through a TCP stream proxy; a web proxy or another device on that port "+
			"(router, UniFi gateway, NAS) answers with its own certificate. Fix the agent address under "+
			"Administration > Agents & pairing and pair again", err, server)
	}
	return err
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
						return &WrongServerError{Presented: describe(cs.PeerCertificates), Err: err}
					}
					*got = c
					return nil
				}
			}
			return &WrongServerError{Presented: describe(cs.PeerCertificates),
				Err: fmt.Errorf("no CA with fingerprint %s in the chain (wrong server, or a man in the middle)", pin)}
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
			if err := verifyChain(cs.PeerCertificates, ca); err != nil {
				return &WrongServerError{Presented: describe(cs.PeerCertificates), Err: err}
			}
			return nil
		},
	}
}
