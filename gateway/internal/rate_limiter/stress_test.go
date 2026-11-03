package ratelimiter

import (
	"context"
	"errors"
	"fmt"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func TestStressRedisLimiterAtomicity(t *testing.T) {
	address := os.Getenv("REDIS_TEST_ADDRESS")
	if address == "" {
		t.Skip("set REDIS_TEST_ADDRESS to run the Redis stress test")
	}

	client := redis.NewClient(&redis.Options{
		Addr:     address,
		Password: os.Getenv("REDIS_TEST_PASSWORD"),
	})
	t.Cleanup(func() {
		_ = client.Close()
	})

	pingContext, pingCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer pingCancel()
	if err := client.Ping(pingContext).Err(); err != nil {
		t.Fatalf("ping Redis: %v", err)
	}

	const workers = 256
	tests := []struct {
		name            string
		policy          Policy
		reservationCost int64
		wantAllowed     int
		wantReason      string
	}{
		{
			name: "requests",
			policy: Policy{
				RequestsPerMinute: 1,
				TokensPerMinute:   1_000_000_000,
				MaxConcurrency:    workers,
				ReservationTTL:    30 * time.Second,
			},
			reservationCost: 1,
			wantAllowed:     1,
			wantReason:      "requests",
		},
		{
			name: "tokens",
			policy: Policy{
				RequestsPerMinute: workers * 2,
				TokensPerMinute:   100,
				MaxConcurrency:    workers,
				ReservationTTL:    30 * time.Second,
			},
			reservationCost: 100,
			wantAllowed:     1,
			wantReason:      "tokens",
		},
		{
			name: "concurrency",
			policy: Policy{
				RequestsPerMinute: workers * 2,
				TokensPerMinute:   1_000_000_000,
				MaxConcurrency:    16,
				ReservationTTL:    30 * time.Second,
			},
			reservationCost: 1,
			wantAllowed:     16,
			wantReason:      "concurrency",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			limiter, err := New(client, test.policy)
			if err != nil {
				t.Fatal(err)
			}

			scope := fmt.Sprintf("rate-limiter-stress:%s:%d", test.name, time.Now().UnixNano())
			requestIDs := make([]string, workers)
			for index := range requestIDs {
				requestIDs[index] = fmt.Sprintf("request-%d", index)
			}
			t.Cleanup(func() {
				keys := redisKeys(scope, requestIDs[0])[:3]
				for _, requestID := range requestIDs {
					keys = append(keys, redisKeys(scope, requestID)[3])
				}
				cleanupContext, cleanupCancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cleanupCancel()
				if err := client.Del(cleanupContext, keys...).Err(); err != nil {
					t.Errorf("clean stress keys: %v", err)
				}
			})

			ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()
			start := make(chan struct{})
			results := make(chan stressResult, workers)
			var waitGroup sync.WaitGroup
			for _, requestID := range requestIDs {
				waitGroup.Add(1)
				go func(requestID string) {
					defer waitGroup.Done()
					<-start
					reservation, err := limiter.Reserve(ctx, scope, requestID, test.reservationCost)
					results <- stressResult{reservation: reservation, err: err}
				}(requestID)
			}
			close(start)
			waitGroup.Wait()
			close(results)

			allowed := 0
			rejected := 0
			for result := range results {
				if result.err == nil {
					allowed++
					if err := limiter.Cancel(ctx, result.reservation); err != nil {
						t.Errorf("cancel reservation: %v", err)
					}
					continue
				}

				var limitError *LimitError
				if !errors.As(result.err, &limitError) {
					t.Errorf("reserve returned unexpected error: %v", result.err)
					continue
				}
				if limitError.Reason != test.wantReason {
					t.Errorf("rejection reason = %q, want %q", limitError.Reason, test.wantReason)
				}
				rejected++
			}

			if allowed != test.wantAllowed {
				t.Errorf("allowed reservations = %d, want %d", allowed, test.wantAllowed)
			}
			if rejected != workers-test.wantAllowed {
				t.Errorf("rejected reservations = %d, want %d", rejected, workers-test.wantAllowed)
			}
		})
	}
}

type stressResult struct {
	reservation Reservation
	err         error
}
