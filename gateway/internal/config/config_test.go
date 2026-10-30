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
	config := []byte("server:\n  host: 0.0.0.0\n  port: \"8000\"\nupstream: http://127.0.0.1:8001\n")
	if err := os.WriteFile(filepath.Join(temporaryDirectory, "config.yaml"), config, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(temporaryDirectory); err != nil {
		t.Fatal(err)
	}
	t.Setenv("UPSTREAM_URL", "http://mock-vllm:8000")

	loaded, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Upstream != "http://mock-vllm:8000" {
		t.Fatalf("upstream = %q, want %q", loaded.Upstream, "http://mock-vllm:8000")
	}
}
