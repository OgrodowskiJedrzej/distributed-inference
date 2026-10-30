package main

import (
	"fmt"
	"log"
	"net"
	"net/http"
	"net/url"
	"time"

	"github.com/ogrodowskijedrzej/inference-lab/internal/api"
	"github.com/ogrodowskijedrzej/inference-lab/internal/config"
)

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return fmt.Errorf("load config: %w", err)
	}

	upstream, err := url.Parse(cfg.Upstream)
	if err != nil || upstream.Scheme == "" || upstream.Host == "" {
		return fmt.Errorf("invalid upstream URL %q", cfg.Upstream)
	}

	address := net.JoinHostPort(cfg.Server.Host, cfg.Server.Port)
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", api.HealthHandler)
	mux.Handle("POST /v1/chat/completions", api.NewChatCompletionsHandler(upstream))

	server := &http.Server{
		Addr:              address,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       2 * time.Minute,
	}

	log.Printf("server is running on %s", address)
	return server.ListenAndServe()
}

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}
