package ratelimiter

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

const minute = 60 * time.Second

var ErrReservationExpired = errors.New("rate limit reservation expired")

type Policy struct {
	RequestsPerMinute int64
	TokensPerMinute   int64
	MaxConcurrency    int64
	ReservationTTL    time.Duration
}

type Reservation struct {
	Scope          string
	ID             string
	ReservedTokens int64
}

type LimitError struct {
	Reason     string
	RetryAfter time.Duration
}

func (e *LimitError) Error() string {
	return fmt.Sprintf("rate limit exceeded: %s", e.Reason)
}

type Limiter struct {
	client redis.UniversalClient
	policy Policy
}

func New(client redis.UniversalClient, policy Policy) (*Limiter, error) {
	if client == nil {
		return nil, errors.New("redis client is required")
	}
	if policy.RequestsPerMinute <= 0 || policy.TokensPerMinute <= 0 || policy.MaxConcurrency <= 0 || policy.ReservationTTL <= 0 {
		return nil, errors.New("rate limit policy values must be positive")
	}
	return &Limiter{client: client, policy: policy}, nil
}

func (l *Limiter) Reserve(ctx context.Context, scope, requestID string, tokens int64) (Reservation, error) {
	if requestID == "" {
		return Reservation{}, errors.New("request ID is required")
	}
	if tokens < 0 {
		return Reservation{}, errors.New("reserved tokens cannot be negative")
	}

	keys := redisKeys(scope, requestID)
	result, err := reserveScript.Run(ctx, l.client, keys,
		l.policy.RequestsPerMinute,
		l.policy.TokensPerMinute,
		l.policy.MaxConcurrency,
		tokens,
		l.policy.ReservationTTL.Milliseconds(),
		minute.Milliseconds(),
	).Slice()
	if err != nil {
		return Reservation{}, fmt.Errorf("reserve rate limit: %w", err)
	}

	allowed, err := resultInt64(result, 0)
	if err != nil {
		return Reservation{}, err
	}
	if allowed == 0 {
		retryMilliseconds, err := resultInt64(result, 2)
		if err != nil {
			return Reservation{}, err
		}
		return Reservation{}, &LimitError{
			Reason:     fmt.Sprint(result[1]),
			RetryAfter: time.Duration(retryMilliseconds) * time.Millisecond,
		}
	}

	return Reservation{Scope: scope, ID: requestID, ReservedTokens: tokens}, nil
}

func (l *Limiter) Reconcile(ctx context.Context, reservation Reservation, actualTokens int64) error {
	if actualTokens < 0 {
		return errors.New("actual tokens cannot be negative")
	}
	return l.finish(ctx, reservation, actualTokens, false)
}

func (l *Limiter) Cancel(ctx context.Context, reservation Reservation) error {
	return l.finish(ctx, reservation, 0, true)
}

func (l *Limiter) Extend(ctx context.Context, reservation Reservation) error {
	keys := redisKeys(reservation.Scope, reservation.ID)
	extended, err := extendScript.Run(ctx, l.client, keys[2:4], l.policy.ReservationTTL.Milliseconds()).Int64()
	if err != nil {
		return fmt.Errorf("extend rate limit reservation: %w", err)
	}
	if extended == 0 {
		return ErrReservationExpired
	}
	return nil
}

func (l *Limiter) finish(ctx context.Context, reservation Reservation, actualTokens int64, cancel bool) error {
	keys := redisKeys(reservation.Scope, reservation.ID)
	cancelValue := 0
	if cancel {
		cancelValue = 1
	}
	if err := finishScript.Run(ctx, l.client, keys,
		l.policy.RequestsPerMinute,
		l.policy.TokensPerMinute,
		actualTokens,
		cancelValue,
		minute.Milliseconds(),
	).Err(); err != nil {
		return fmt.Errorf("finish rate limit reservation: %w", err)
	}
	return nil
}

func redisKeys(scope, requestID string) []string {
	hash := fmt.Sprintf("%x", sha256.Sum256([]byte(scope)))
	prefix := "rate_limit:{" + hash + "}:"
	return []string{
		prefix + "requests",
		prefix + "tokens",
		prefix + "concurrency",
		prefix + "reservation:" + requestID,
	}
}

func resultInt64(result []any, index int) (int64, error) {
	if index >= len(result) {
		return 0, errors.New("invalid rate limit script response")
	}
	value, ok := result[index].(int64)
	if !ok {
		return 0, errors.New("invalid rate limit script response")
	}
	return value, nil
}
