package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHealthHandler(t *testing.T) {
	request := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	response := httptest.NewRecorder()

	HealthHandler(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusOK)
	}
	if response.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("content type = %q, want %q", response.Header().Get("Content-Type"), "application/json")
	}
	if response.Body.String() != "{\"message\":\"Healthy\"}\n" {
		t.Fatalf("body = %q, want health response", response.Body.String())
	}
}
