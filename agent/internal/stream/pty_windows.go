//go:build windows

package stream

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"sync"
	"time"
	"unsafe"

	"github.com/coder/websocket"
	"golang.org/x/sys/windows"

	"github.com/rhisijmuiden/stackpilot/agent/internal/winps"
)

// servePTY runs PowerShell in a Windows pseudo console (ConPTY, Windows 10 1809 / Server 2019 and
// newer) and relays it over the WebSocket, the same way the Linux agent relays bash.
func servePTY(ctx context.Context, c *websocket.Conn, r *Request) error {
	cmdline := `"` + winps.Exe() + `" -NoLogo -ExecutionPolicy Bypass`
	cleanup := func() {}
	switch r.Mode {
	case "node-shell":
	case "script":
		f, err := os.CreateTemp("", "mmn-run-*.ps1")
		if err != nil {
			sendExit(ctx, c, 1, err.Error())
			return err
		}
		_, _ = f.WriteString(r.Script)
		f.Close()
		cleanup = func() { os.Remove(f.Name()) }
		cmdline += ` -NoProfile -File "` + f.Name() + `"`
	default:
		sendExit(ctx, c, 1, "this terminal is not available on Windows")
		return errors.New("unsupported mode")
	}
	defer cleanup()
	cols, rows := r.Cols, r.Rows
	if cols <= 0 || cols > 1000 {
		cols = 120
	}
	if rows <= 0 || rows > 500 {
		rows = 32
	}
	p, err := startConPTY(cmdline, int16(cols), int16(rows))
	if err != nil {
		sendExit(ctx, c, 1, "could not start PowerShell: "+err.Error())
		return err
	}
	defer p.Close()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	exited := make(chan int, 1)
	go func() {
		exited <- p.Wait()
		// Closing the pseudo console ends the output pipe, so the reader below stops.
		p.closeConsole()
	}()
	go func() {
		buf := make([]byte, 32*1024)
		for {
			n, err := p.out.Read(buf)
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
		p.Kill()
	}()
	for {
		typ, data, err := c.Read(ctx)
		if err != nil {
			return nil
		}
		if typ == websocket.MessageBinary {
			if _, err := p.in.Write(data); err != nil {
				return err
			}
			continue
		}
		var m control
		if json.Unmarshal(data, &m) == nil && m.Type == "resize" && m.Cols > 0 && m.Rows > 0 && m.Cols < 1000 && m.Rows < 500 {
			_ = windows.ResizePseudoConsole(p.hpc, windows.Coord{X: int16(m.Cols), Y: int16(m.Rows)})
		}
	}
}

type conPTY struct {
	hpc      windows.Handle
	process  windows.Handle
	thread   windows.Handle
	in       *os.File
	out      *os.File
	once     sync.Once
	attrList *windows.ProcThreadAttributeListContainer
}

func startConPTY(cmdline string, cols, rows int16) (*conPTY, error) {
	var inR, inW, outR, outW windows.Handle
	if err := windows.CreatePipe(&inR, &inW, nil, 0); err != nil {
		return nil, err
	}
	if err := windows.CreatePipe(&outR, &outW, nil, 0); err != nil {
		windows.CloseHandle(inR)
		windows.CloseHandle(inW)
		return nil, err
	}
	var hpc windows.Handle
	if err := windows.CreatePseudoConsole(windows.Coord{X: cols, Y: rows}, inR, outW, 0, &hpc); err != nil {
		for _, h := range []windows.Handle{inR, inW, outR, outW} {
			windows.CloseHandle(h)
		}
		return nil, errors.New("this Windows version has no pseudo console (needs Windows 10 1809 / Server 2019 or newer)")
	}
	// The pseudo console holds its own copies of these ends.
	windows.CloseHandle(inR)
	windows.CloseHandle(outW)
	p := &conPTY{hpc: hpc, in: os.NewFile(uintptr(inW), "conin"), out: os.NewFile(uintptr(outR), "conout")}
	attr, err := windows.NewProcThreadAttributeList(1)
	if err != nil {
		p.Close()
		return nil, err
	}
	p.attrList = attr
	// The attribute value is the HPCON itself, not a pointer to it.
	if err := attr.Update(windows.PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, *(*unsafe.Pointer)(unsafe.Pointer(&hpc)), unsafe.Sizeof(hpc)); err != nil {
		p.Close()
		return nil, err
	}
	si := &windows.StartupInfoEx{ProcThreadAttributeList: attr.List()}
	si.StartupInfo.Cb = uint32(unsafe.Sizeof(*si))
	// Without this the child inherits the service's (redirected) standard handles instead of the console.
	si.StartupInfo.Flags = windows.STARTF_USESTDHANDLES
	cl, err := windows.UTF16PtrFromString(cmdline)
	if err != nil {
		p.Close()
		return nil, err
	}
	dir, _ := windows.UTF16PtrFromString(systemRoot())
	env := envBlock()
	var pi windows.ProcessInformation
	if err := windows.CreateProcess(nil, cl, nil, nil, false,
		windows.EXTENDED_STARTUPINFO_PRESENT|windows.CREATE_UNICODE_ENVIRONMENT, env, dir, &si.StartupInfo, &pi); err != nil {
		p.Close()
		return nil, err
	}
	p.process, p.thread = pi.Process, pi.Thread
	return p, nil
}

func systemRoot() string {
	if d := os.Getenv("SystemDrive"); d != "" {
		return d + `\`
	}
	return `C:\`
}

// envBlock is the agent's environment plus TERM, as a UTF-16 block for CreateProcess.
func envBlock() *uint16 {
	vars := append(os.Environ(), "TERM=xterm-256color")
	var b []uint16
	for _, v := range vars {
		if strings.HasPrefix(v, "=") {
			continue
		}
		u, err := windows.UTF16FromString(v)
		if err != nil {
			continue
		}
		b = append(b, u...)
	}
	b = append(b, 0)
	return &b[0]
}

func (p *conPTY) Wait() int {
	if p.process == 0 {
		return -1
	}
	_, _ = windows.WaitForSingleObject(p.process, windows.INFINITE)
	var code uint32
	if windows.GetExitCodeProcess(p.process, &code) != nil {
		return -1
	}
	return int(code)
}

func (p *conPTY) Kill() {
	if p.process != 0 {
		_ = windows.TerminateProcess(p.process, 1)
	}
}

func (p *conPTY) closeConsole() {
	p.once.Do(func() { windows.ClosePseudoConsole(p.hpc) })
}

func (p *conPTY) Close() {
	p.closeConsole()
	if p.attrList != nil {
		p.attrList.Delete()
	}
	if p.process != 0 {
		windows.CloseHandle(p.process)
	}
	if p.thread != 0 {
		windows.CloseHandle(p.thread)
	}
	p.in.Close()
	p.out.Close()
}
