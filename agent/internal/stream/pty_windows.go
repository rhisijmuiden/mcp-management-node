//go:build windows

package stream

import (
	"context"
	"errors"

	"github.com/coder/websocket"
)

func servePTY(ctx context.Context, c *websocket.Conn, r *Request) error {
	sendExit(ctx, c, 1, "terminals are not supported on Windows yet")
	return errors.New("not supported")
}
