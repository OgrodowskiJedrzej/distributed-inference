package loadbalancer

import (
	"errors"
	"sync"

	"github.com/ogrodowskijedrzej/inference-lab/internal/backend"
)

var ErrNoBackends = errors.New("no backends configured")

type RoundRobin struct {
	pool *backend.Pool
	mu   sync.Mutex
	next int
}

func NewRoundRobin(pool *backend.Pool) *RoundRobin {
	return &RoundRobin{pool: pool}
}

func (r *RoundRobin) Next() (string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	if r.pool.Len() == 0 {
		return "", ErrNoBackends
	}

	address := r.pool.Address(r.next % r.pool.Len())
	r.next++

	return address, nil
}
