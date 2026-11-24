package api

import (
	"encoding/json"
	"log"
	"net/http"
)

type healthResponse struct {
	Message string `json:"message"`
}

func HealthHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(healthResponse{Message: "Healthy"}); err != nil {
		log.Printf("failed to encode json: %v", err)
	}
}
