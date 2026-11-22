//go:build !windows

package transfer

import (
	"archive/tar"
	"io/fs"
	"os"
	"syscall"
)

func fillOwner(h *tar.Header, fi fs.FileInfo) {
	if st, ok := fi.Sys().(*syscall.Stat_t); ok {
		h.Uid, h.Gid = int(st.Uid), int(st.Gid)
	}
}

// setOwner keeps the numeric owner when the agent runs as root (it does as a service).
func setOwner(path string, h *tar.Header) {
	if os.Geteuid() == 0 {
		_ = os.Lchown(path, h.Uid, h.Gid)
	}
}
