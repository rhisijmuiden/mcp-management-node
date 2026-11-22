//go:build !windows

package main

import (
	"context"
	"log"
)

const serviceUsage = ""

func runPlatform(fn func(ctx context.Context)) bool { return false }

func serviceCommand(args []string) {
	log.Fatal("the service command is for Windows; on Linux systemd runs 'mmn-agent run'")
}
