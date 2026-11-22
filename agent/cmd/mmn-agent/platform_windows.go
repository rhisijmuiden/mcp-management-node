//go:build windows

package main

import (
	"context"
	"fmt"
	"log"
	"os"

	"github.com/rhisijmuiden/mcp-management-node/agent/internal/client"
	"github.com/rhisijmuiden/mcp-management-node/agent/internal/config"
	"github.com/rhisijmuiden/mcp-management-node/agent/internal/winsvc"
)

const serviceUsage = "  mmn-agent service install|uninstall   (register the Windows service)\n"

// runPlatform runs the agent under the Service Control Manager when Windows started it.
func runPlatform(fn func(ctx context.Context)) bool {
	if !winsvc.IsService() {
		return false
	}
	// Exit with an error after a self-update so the service recovery restarts the new version.
	client.RestartExitCode = 1
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
