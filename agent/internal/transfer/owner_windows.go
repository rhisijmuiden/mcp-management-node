//go:build windows

package transfer

import (
	"archive/tar"
	"io/fs"
)

func fillOwner(h *tar.Header, fi fs.FileInfo) {}

func setOwner(path string, h *tar.Header) {}
