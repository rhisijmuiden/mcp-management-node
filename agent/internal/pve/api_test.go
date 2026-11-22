package pve

import (
	"context"
	"strings"
	"testing"
)

func TestBuildArgs(t *testing.T) {
	got, err := buildArgs(map[string]any{"memory": float64(2048), "start": true, "net0": "virtio,bridge=vmbr0",
		"ssh-public-keys": "ssh-ed25519 AAA x", "skip": nil, "tag": []any{"a", "b"}})
	if err != nil {
		t.Fatal(err)
	}
	want := "--memory 2048 --net0 virtio,bridge=vmbr0 --ssh-public-keys ssh-ed25519 AAA x --start 1 --tag a --tag b"
	if strings.Join(got, " ") != want {
		t.Fatalf("got %q", strings.Join(got, " "))
	}
	if _, err := buildArgs(map[string]any{"--evil": "x"}); err == nil {
		t.Fatal("bad key accepted")
	}
}

func TestAPIValidation(t *testing.T) {
	if _, err := API(context.Background(), "get", "/nodes/../etc", nil, 0); err == nil {
		t.Fatal("path with .. accepted")
	}
	if _, err := API(context.Background(), "exec", "/nodes", nil, 0); err == nil {
		t.Fatal("bad method accepted")
	}
}

func TestListGuestsFiltersLocalNode(t *testing.T) {
	Pvesh = func(ctx context.Context, args ...string) ([]byte, error) {
		if args[1] == "/cluster/status" {
			return []byte(`[{"type":"node","name":"pve01","online":1,"local":1}]`), nil
		}
		return []byte(`[{"type":"qemu","vmid":101,"name":"web","node":"pve01","status":"running","maxmem":2147483648,"tags":"prod"},
			{"type":"lxc","vmid":100,"name":"dns","node":"pve01","status":"stopped"},
			{"type":"lxc","vmid":200,"name":"other","node":"pve02","status":"running"},
			{"type":"storage","id":"storage/pve01/local","node":"pve01"}]`), nil
	}
	gl, err := ListGuests(context.Background())
	if err != nil || gl.Node != "pve01" || len(gl.Guests) != 2 || gl.Guests[0].VMID != 100 || gl.Guests[1].Tags != "prod" {
		t.Fatalf("bad: %v %+v", err, gl)
	}
}

func TestRunTaskFollowsLog(t *testing.T) {
	Available = func() bool { return true }
	calls := 0
	Pvesh = func(ctx context.Context, args ...string) ([]byte, error) {
		switch {
		case args[0] == "create":
			return []byte(`"UPID:pve01:0001:0002:0003:qmstart:101:root@pam:"`), nil
		case strings.HasSuffix(args[1], "/log"):
			return []byte(`[{"n":1,"t":"starting"},{"n":2,"t":"TASK OK"}]`), nil
		case strings.HasSuffix(args[1], "/status"):
			calls++
			return []byte(`{"status":"stopped","exitstatus":"OK"}`), nil
		}
		return []byte(`null`), nil
	}
	var lines []string
	res, err := RunTask(context.Background(), "create", "/nodes/localhost/qemu/101/status/start", nil,
		func(s string) { lines = append(lines, s) })
	if err != nil || res["upid"] == nil || len(lines) != 3 || calls != 1 {
		t.Fatalf("bad: %v %v %v", err, res, lines)
	}
}
