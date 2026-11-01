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
	"github.com/ogrodowskijedrzej/inference-lab/internal/observability"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
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
	registry := prometheus.NewRegistry()
	registry.MustRegister(
		collectors.NewGoCollector(),
		collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
	)
	httpMetrics := observability.NewHTTPMetrics(registry)

	mux := http.NewServeMux()
	mux.Handle("GET /healthz", httpMetrics.Instrument("/healthz", http.HandlerFunc(api.HealthHandler)))
	mux.Handle("POST /v1/chat/completions", httpMetrics.Instrument("/v1/chat/completions", api.NewChatCompletionsHandler(upstream)))
	mux.Handle("GET /metrics", promhttp.HandlerFor(registry, promhttp.HandlerOpts{}))

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
