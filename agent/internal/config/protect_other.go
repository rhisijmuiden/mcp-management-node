//go:build !windows

package config

import "os"

// Protect makes sure a private folder exists (mode 0700; the parent folders are root's).
func Protect(dir string) error {
	return os.MkdirAll(dir, 0o700)
}
