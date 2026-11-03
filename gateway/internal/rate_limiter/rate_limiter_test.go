package ratelimiter

import (
	"testing"
	"time"
)

func TestRedisKeysUseOneClusterSlotAndHideScope(t *testing.T) {
	keys := redisKeys("tenant-a:model-a", "request-1")
	if len(keys) != 4 {
		t.Fatalf("key count = %d, want 4", len(keys))
	}

	wantPrefix := "rate_limit:{7ff8319df0dac0208aa8798a336c080ecd65527d597cd25b1101c9a952184418}:"
	for _, key := range keys {
		if len(key) < len(wantPrefix) || key[:len(wantPrefix)] != wantPrefix {
			t.Fatalf("key = %q, want prefix %q", key, wantPrefix)
		}
	}
}

func TestLimitError(t *testing.T) {
	err := &LimitError{Reason: "tokens", RetryAfter: 2 * time.Second}
	if err.Error() != "rate limit exceeded: tokens" {
		t.Fatalf("error = %q", err.Error())
	}
}
