// mmn-agent connects a Proxmox VE node (or another Linux / Windows server) to MCP Management Node.
//
//	mmn-agent enroll --server mmn.example.nl:8443 --fingerprint <sha256> --code ABCD-EFGH-JKLM
//	mmn-agent run          (what the systemd service runs)
//	mmn-agent status
//	mmn-agent version
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/rhisijmuiden/mcp-management-node/agent/internal/client"
	"github.com/rhisijmuiden/mcp-management-node/agent/internal/config"
	"github.com/rhisijmuiden/mcp-management-node/agent/internal/enroll"
)

// version is set at build time: -ldflags "-X main.version=0.4.0".
var version = "dev"

func usage() {
	fmt.Fprintf(os.Stderr, `mmn-agent %s: agent for MCP Management Node

Usage:
  mmn-agent enroll --server HOST[:PORT] --fingerprint SHA256 --code CODE [--name NAME]
  mmn-agent run
  mmn-agent status
  mmn-agent unenroll
  mmn-agent version
`, version)
}

func main() {
	log.SetFlags(0)
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "version", "--version", "-v":
		fmt.Println(version)
	case "enroll":
		fs := flag.NewFlagSet("enroll", flag.ExitOnError)
		server := fs.String("server", "", "agent endpoint of MCP Management Node (host:port)")
		fp := fs.String("fingerprint", "", "SHA-256 fingerprint of the app's agent CA")
		code := fs.String("code", "", "one-time pairing code from the app")
		name := fs.String("name", "", "name to report (default: host name)")
		force := fs.Bool("force", false, "enroll again even if already enrolled")
		_ = fs.Parse(os.Args[2:])
		if _, err := config.Load(); err == nil && !*force {
			log.Fatal("already enrolled; use --force to pair again with a new code")
		}
		res, err := enroll.Run(*server, *fp, *code, *name, version)
		if err != nil {
			log.Fatalf("enrollment failed: %v", err)
		}
		fmt.Printf("Enrolled as node %q in environment %q (id %s).\n", res.NodeName, res.Environment, res.NodeID)
	case "run":
		run()
	case "status":
		cfg, err := config.Load()
		if err != nil {
			fmt.Println(err)
			os.Exit(1)
		}
		fmt.Printf("version:  %s\nserver:   %s\nnode id:  %s\nca:       sha256:%s\n", version, cfg.Server, cfg.NodeID, cfg.CAFingerprint)
	case "unenroll":
		if err := config.Remove(); err != nil {
			log.Fatal(err)
		}
		fmt.Println("enrollment removed")
	case "help", "-h", "--help":
		usage()
	default:
		usage()
		os.Exit(2)
	}
}

func run() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	log.SetFlags(log.LstdFlags)
	log.Printf("mmn-agent %s starting", version)
	for {
		a, err := client.New(version)
		if err == nil {
			if err := a.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
				log.Fatal(err)
			}
			return
		}
		if !errors.Is(err, config.ErrNotEnrolled) {
			log.Fatal(err)
		}
		// Not paired yet: wait for 'mmn-agent enroll' instead of crash-looping.
		log.Print(err)
		select {
		case <-ctx.Done():
			return
		case <-time.After(15 * time.Second):
		}
	}
}
