// Package proctree makes a command stop together with the processes it started.
//
// exec.CommandContext only kills the direct child when the context ends. A shell or PowerShell
// that starts other programs (DISM, msiexec, robocopy, apt, ...) leaves those running, and as long
// as they hold the output pipes cmd.Wait does not return either: the caller sees a time-out while
// the work goes on unnoticed. Setup kills the whole tree and bounds the wait for the pipes.
package proctree

import (
	"os/exec"
	"time"
)

// WaitDelay is how long Wait waits for the output pipes after the process exited or was killed.
const WaitDelay = 10 * time.Second

// Setup prepares cmd (before Start) so that cancelling its context kills the process and all
// processes it started, and so that Wait returns at most WaitDelay after that.
func Setup(cmd *exec.Cmd) {
	setup(cmd)
	cmd.WaitDelay = WaitDelay
}
