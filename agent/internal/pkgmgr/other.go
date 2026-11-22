//go:build !windows

package pkgmgr

import (
	"context"
	"errors"

	"github.com/rhisijmuiden/stackpilot/agent/internal/proto"
)

const isWindows = false

func winCheck(ctx context.Context) proto.Updates {
	return proto.Updates{CheckedAt: now(), Packages: []proto.Package{}, Error: "not Windows"}
}

func winApply(ctx context.Context, line func(string)) error { return errors.New("not Windows") }
