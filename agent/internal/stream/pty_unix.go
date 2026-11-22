//go:build !windows

package stream

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"strconv"
	"syscall"
	"time"

	"github.com/coder/websocket"
	"github.com/creack/pty"
)

func command(r *Request) (*exec.Cmd, func(), error) {
	cleanup := func() {}
	var cmd *exec.Cmd
	switch r.Mode {
	case "lxc-console":
		// Same as the Proxmox VE web console: the container's own login prompt, escape disabled.
		cmd = exec.Command("lxc-console", "-n", strconv.Itoa(r.VMID), "-e", "-1")
	case "lxc-shell":
		cmd = exec.Command("pct", "enter", strconv.Itoa(r.VMID))
	case "node-shell":
		cmd = exec.Command("/bin/bash", "-l")
	case "script":
		f, err := os.CreateTemp("", "mmn-run-*.sh")
		if err != nil {
			return nil, cleanup, err
		}
		if _, err := f.WriteString(r.Script); err != nil {
			f.Close()
			return nil, cleanup, err
		}
		f.Close()
		_ = os.Chmod(f.Name(), 0o700)
		cleanup = func() { os.Remove(f.Name()) }
		cmd = exec.Command("/bin/bash", f.Name())
	default:
		return nil, cleanup, errors.New("unknown mode")
	}
	cmd.Env = append(os.Environ(), "TERM=xterm-256color", "LANG=C.UTF-8", "HOME=/root")
	for k, v := range r.Env {
		if envKeyOK(k) {
			cmd.Env = append(cmd.Env, k+"="+v)
		}
	}
	cmd.Dir = "/root"
	return cmd, cleanup, nil
}

func envKeyOK(k string) bool {
	if k == "" || len(k) > 64 {
		return false
	}
	for i, ch := range k {
		if !(ch == '_' || ch >= 'A' && ch <= 'Z' || ch >= 'a' && ch <= 'z' || i > 0 && ch >= '0' && ch <= '9') {
			return false
		}
	}
	return true
}

func servePTY(ctx context.Context, c *websocket.Conn, r *Request) error {
	cmd, cleanup, err := command(r)
	defer cleanup()
	if err != nil {
		sendExit(ctx, c, 1, err.Error())
		return err
	}
	cols, rows := r.Cols, r.Rows
	if cols <= 0 || cols > 1000 {
		cols = 120
	}
	if rows <= 0 || rows > 500 {
		rows = 32
	}
	f, err := pty.StartWithSize(cmd, &pty.Winsize{Cols: uint16(cols), Rows: uint16(rows)})
	if err != nil {
		sendExit(ctx, c, 1, err.Error())
		return err
	}
	defer f.Close()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	exited := make(chan int, 1)
	go func() {
		err := cmd.Wait()
		code := 0
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			code = ee.ExitCode()
		} else if err != nil {
			code = -1
		}
		exited <- code
	}()
	go func() {
		buf := make([]byte, 32*1024)
		for {
			n, err := f.Read(buf)
			if n > 0 {
				if werr := c.Write(ctx, websocket.MessageBinary, buf[:n]); werr != nil {
					cancel()
					return
				}
			}
			if err != nil {
				select {
				case code := <-exited:
					sendExit(ctx, c, code, "")
				case <-time.After(3 * time.Second):
					sendExit(ctx, c, -1, "")
				}
				cancel()
				return
			}
		}
	}()
	go func() {
		<-ctx.Done()
		if cmd.Process != nil {
			_ = cmd.Process.Signal(syscall.SIGHUP)
			time.AfterFunc(5*time.Second, func() { _ = cmd.Process.Kill() })
		}
	}()
	for {
		typ, data, err := c.Read(ctx)
		if err != nil {
			return nil
		}
		if typ == websocket.MessageBinary {
			if _, err := f.Write(data); err != nil {
				return err
			}
			continue
		}
		var m control
		if json.Unmarshal(data, &m) == nil && m.Type == "resize" && m.Cols > 0 && m.Rows > 0 && m.Cols < 1000 && m.Rows < 500 {
			_ = pty.Setsize(f, &pty.Winsize{Cols: uint16(m.Cols), Rows: uint16(m.Rows)})
		}
	}
}
