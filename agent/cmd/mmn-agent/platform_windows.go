//go:build windows

package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"time"

	"github.com/rhisijmuiden/stackpilot/agent/internal/client"
	"github.com/rhisijmuiden/stackpilot/agent/internal/config"
	"github.com/rhisijmuiden/stackpilot/agent/internal/winsvc"
)

const serviceUsage = "  mmn-agent service install|uninstall   (register the Windows service)\n"

// runPlatform runs the agent under the Service Control Manager when Windows started it.
func runPlatform(fn func(ctx context.Context)) bool {
	if !winsvc.IsService() {
		return false
	}
	// After a self-update the agent exits with an error: the service recovery actions (set on
	// every start) restart it with the new version within seconds. Windows logs that as event
	// 7031; it is expected. A scheduled task armed just before is the safety net in case the
	// recovery does not start it.
	client.RestartExitCode = 1
	client.BeforeRestart = func() {
		if err := winsvc.ArmRestartTask(); err != nil {
			log.Printf("restart safety task: %v", err)
		}
	}
	go func() { time.Sleep(3 * time.Minute); winsvc.DisarmRestartTask() }()
	if err := winsvc.Run(config.StateDir, fn); err != nil {
		log.Printf("service: %v", err)
	}
	return true
}

func serviceCommand(args []string) {
	if len(args) == 0 {
		fmt.Fprint(os.Stderr, serviceUsage)
		os.Exit(2)
	}
	var err error
	switch args[0] {
	case "install":
		exe, e := os.Executable()
		if e != nil {
			log.Fatal(e)
		}
		err = winsvc.Install(exe)
	case "uninstall":
		err = winsvc.Uninstall()
	default:
		fmt.Fprint(os.Stderr, serviceUsage)
		os.Exit(2)
	}
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println("done")
}
