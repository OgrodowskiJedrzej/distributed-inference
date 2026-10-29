package loadbalancer

import (
	"testing"

	"github.com/ogrodowskijedrzej/inference-lab/internal/backend"
)

func TestRoundRobinNext(t *testing.T) {
	pool := backend.NewPool([]string{
		"http://worker-0:8000",
		"http://worker-1:8000",
	})
	balancer := NewRoundRobin(pool)

	want := []string{
		"http://worker-0:8000",
		"http://worker-1:8000",
		"http://worker-0:8000",
	}
	for _, expected := range want {
		got, err := balancer.Next()
		if err != nil {
			t.Fatal(err)
		}
		if got != expected {
			t.Fatalf("Next() = %q, want %q", got, expected)
		}
	}
}

func TestRoundRobinNextWithoutBackends(t *testing.T) {
	balancer := NewRoundRobin(backend.NewPool(nil))

	if _, err := balancer.Next(); err != ErrNoBackends {
		t.Fatalf("Next() error = %v, want %v", err, ErrNoBackends)
	}
}
