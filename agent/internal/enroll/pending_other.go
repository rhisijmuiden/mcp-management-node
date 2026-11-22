//go:build !windows

package enroll

func loadPlatformPending() *Pending { return nil }

func clearPlatformPending() {}
