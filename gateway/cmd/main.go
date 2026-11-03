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
	ratelimiter "github.com/ogrodowskijedrzej/inference-lab/internal/rate_limiter"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/redis/go-redis/v9"
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
	redisClient := redis.NewClient(&redis.Options{
		Addr:     cfg.Redis.Address,
		Password: cfg.Redis.Password,
		DB:       cfg.Redis.DB,
	})
	defer redisClient.Close()

	limiter, err := ratelimiter.New(redisClient, ratelimiter.Policy{
		RequestsPerMinute: cfg.RateLimit.RequestsPerMinute,
		TokensPerMinute:   cfg.RateLimit.TokensPerMinute,
		MaxConcurrency:    cfg.RateLimit.MaxConcurrency,
		ReservationTTL:    cfg.RateLimit.ReservationTTL,
	})
	if err != nil {
		return fmt.Errorf("create rate limiter: %w", err)
	}
	rateLimitMiddleware, err := ratelimiter.NewHTTPMiddleware(
		limiter,
		cfg.RateLimit.ReservationTTL,
		cfg.RateLimit.DefaultMaxTokens,
		ratelimiter.ModelScope,
	)
	if err != nil {
		return fmt.Errorf("create rate limit middleware: %w", err)
	}

	mux := http.NewServeMux()
	mux.Handle("GET /healthz", httpMetrics.Instrument("/healthz", http.HandlerFunc(api.HealthHandler)))
	mux.Handle("POST /v1/chat/completions", httpMetrics.Instrument("/v1/chat/completions", rateLimitMiddleware.Wrap(api.NewChatCompletionsHandler(upstream))))
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
