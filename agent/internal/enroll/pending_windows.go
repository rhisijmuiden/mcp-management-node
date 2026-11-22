//go:build windows

package enroll

import "golang.org/x/sys/windows/registry"

// RegistryKey holds the pairing written by the MSI (HKLM, 64-bit view). It keeps the product's
// former name (MCP Management Node) on purpose, so agents installed before the rename keep pairing.
const RegistryKey = `SOFTWARE\MCP Management Node\Agent`

func loadPlatformPending() *Pending {
	for _, view := range []uint32{registry.WOW64_64KEY, registry.WOW64_32KEY} {
		k, err := registry.OpenKey(registry.LOCAL_MACHINE, RegistryKey, registry.QUERY_VALUE|view)
		if err != nil {
			continue
		}
		get := func(n string) string { s, _, _ := k.GetStringValue(n); return s }
		p := &Pending{Server: get("PairServer"), Fingerprint: get("PairFingerprint"), Code: get("PairCode"), Name: get("PairName")}
		k.Close()
		if p.complete() {
			return p
		}
	}
	return nil
}

func clearPlatformPending() {
	for _, view := range []uint32{registry.WOW64_64KEY, registry.WOW64_32KEY} {
		if k, err := registry.OpenKey(registry.LOCAL_MACHINE, RegistryKey, registry.SET_VALUE|view); err == nil {
			_ = k.DeleteValue("PairCode")
			k.Close()
		}
	}
}
