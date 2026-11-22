package pve

import (
	"context"
	"errors"
	"strings"
	"testing"
)

func TestClusterStatus(t *testing.T) {
	Pvesh = func(ctx context.Context, args ...string) ([]byte, error) {
		return []byte(`[{"type":"cluster","name":"home","quorate":1,"nodes":2},
			{"type":"node","name":"pve01","online":1,"local":1,"ip":"10.0.0.1"},
			{"type":"node","name":"pve02","online":0,"local":0,"ip":"10.0.0.2"}]`), nil
	}
	c, err := ClusterStatus(context.Background())
	if err != nil || c.Name != "home" || !c.Quorate || len(c.Nodes) != 2 || !c.Nodes[0].Local || c.Nodes[1].Online {
		t.Fatalf("bad: %v %+v", err, c)
	}
}

func TestGuests(t *testing.T) {
	Pvesh = func(ctx context.Context, args ...string) ([]byte, error) {
		if strings.HasSuffix(args[1], "qemu") {
			return []byte(`[{"status":"running"},{"status":"stopped"}]`), nil
		}
		return []byte(`[{"status":"running"},{"status":"running"},{"status":"stopped"}]`), nil
	}
	g, err := Guests(context.Background())
	if err != nil || g.VMsRunning != 1 || g.VMsTotal != 2 || g.CTsRunning != 2 || g.CTsTotal != 3 {
		t.Fatalf("bad: %v %+v", err, g)
	}
}

func TestRemoveNotifyOnlyExisting(t *testing.T) {
	var calls []string
	Pvesh = func(ctx context.Context, args ...string) ([]byte, error) {
		calls = append(calls, strings.Join(args[:2], " "))
		if args[0] == "get" && strings.HasSuffix(args[1], "mmn-vzdump") {
			return []byte(`{}`), nil
		}
		if args[0] == "get" {
			return nil, errors.New("not found")
		}
		return nil, nil
	}
	if err := RemoveNotify(context.Background(), "mmn-smtp"); err != nil {
		t.Fatal(err)
	}
	deletes := 0
	for _, c := range calls {
		if strings.HasPrefix(c, "delete") {
			deletes++
		}
	}
	if deletes != 1 {
		t.Fatalf("expected 1 delete, calls: %v", calls)
	}
}
