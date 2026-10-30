package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"

	"github.com/ogrodowskijedrzej/inference-lab/internal/backend"
	"github.com/ogrodowskijedrzej/inference-lab/internal/config"
	"github.com/ogrodowskijedrzej/inference-lab/internal/loadbalancer"
)

type Response struct {
	Message string `json:"message"`
}

type ChatCompletionRequest struct {
	// minimal implementation for now
	Model string `json:"model"`
}

func healthHandler(w http.ResponseWriter, r *http.Request) {
	resp := Response{Message: "Healthy"}
	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(resp); err != nil {
		log.Printf("failed to encode json: %v", err)
		http.Error(w, "failed", http.StatusBadRequest)
		return
	}
}

func inputHandler(balancer *loadbalancer.RoundRobin) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		backendURL, err := balancer.Next()
		if err != nil {
			http.Error(w, "no backends available", http.StatusServiceUnavailable)
			return
		}

		target, err := url.Parse(backendURL)
		if err != nil {
			http.Error(w, "no backends available", http.StatusServiceUnavailable)
			return
		}
		httputil.NewSingleHostReverseProxy(target).ServeHTTP(w, r)
	}
}

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatal(err)
	}
	address := net.JoinHostPort(cfg.Server.Host, cfg.Server.Port)
	pool := backend.NewPool(cfg.Backends)
	balancer := loadbalancer.NewRoundRobin(pool)
	http.HandleFunc("GET /healthz", healthHandler)
	http.HandleFunc("POST /v1/chat/completions", inputHandler(balancer))
	fmt.Printf("Server is running on %s\n", address)
	log.Fatal(http.ListenAndServe(address, nil))
}
