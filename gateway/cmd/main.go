package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"

	"github.com/ogrodowskijedrzej/inference-lab/internal/config"
)

type Response struct {
	Message string `json:"message"`
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

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatal(err)
	}
	address := net.JoinHostPort(cfg.Server.Host, cfg.Server.Port)

	http.HandleFunc("GET /healthz", healthHandler)
	fmt.Printf("Server is running on %s\n", address)
	log.Fatal(http.ListenAndServe(address, nil))
}
