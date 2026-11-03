package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadUsesUpstreamFromEnvironment(t *testing.T) {
	workingDirectory, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.Chdir(workingDirectory); err != nil {
			t.Errorf("restore working directory: %v", err)
		}
	})

	temporaryDirectory := t.TempDir()
	config := []byte("server:\n  host: 0.0.0.0\n  port: \"8000\"\nredis:\n  address: redis:6379\n  password: \"\"\n  db: 0\nrate_limit:\n  requests_per_minute: 60\n  tokens_per_minute: 100000\n  max_concurrency: 8\n  default_max_tokens: 512\n  reservation_ttl: 10m\nupstream: http://127.0.0.1:8001\n")
	if err := os.WriteFile(filepath.Join(temporaryDirectory, "config.yaml"), config, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(temporaryDirectory); err != nil {
		t.Fatal(err)
	}
	t.Setenv("UPSTREAM_URL", "http://mock-vllm:8000")
	t.Setenv("REDIS_ADDRESS", "inference-redis-master:6379")
	t.Setenv("REDIS_PASSWORD", "test-password")
	loaded, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Upstream != "http://mock-vllm:8000" {
		t.Fatalf("upstream = %q, want %q", loaded.Upstream, "http://mock-vllm:8000")
	}
	if loaded.RateLimit.ReservationTTL.String() != "10m0s" {
		t.Fatalf("reservation TTL = %s, want 10m", loaded.RateLimit.ReservationTTL)
	}
	if loaded.RateLimit.DefaultMaxTokens != 512 {
		t.Fatalf("default max tokens = %d, want 512", loaded.RateLimit.DefaultMaxTokens)
	}
	if loaded.Redis.Address != "inference-redis-master:6379" {
		t.Fatalf("upstream = %q, want %q", loaded.Redis.Address, "inference-redis-master:6379")
	}
	if loaded.Redis.Password != "test-password" {
		t.Fatalf("upstream = %q, want %q", loaded.Redis.Password, "test-password")
	}
}
