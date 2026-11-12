package observability

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestRequestLoggerReplacesAndPropagatesRequestID(t *testing.T) {
	var output bytes.Buffer
	var requestID string
	logger := slog.New(slog.NewJSONHandler(&output, nil))
	handler := RequestLogger(logger, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestID = r.Header.Get(RequestIDHeader)
		if requestID == "" || requestID == "client-request-id" {
			t.Fatalf("request ID = %q, want a generated ID", requestID)
		}
		w.WriteHeader(http.StatusAccepted)
	}))
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	request.Header.Set(RequestIDHeader, "client-request-id")
	response := httptest.NewRecorder()

	handler.ServeHTTP(response, request)

	if response.Header().Get(RequestIDHeader) != requestID {
		t.Fatalf("response request ID = %q, want %q", response.Header().Get(RequestIDHeader), requestID)
	}
	var record map[string]any
	if err := json.Unmarshal(output.Bytes(), &record); err != nil {
		t.Fatal(err)
	}
	if record["component"] != "gateway" || record["event"] != "request_completed" || record["request_id"] != requestID {
		t.Fatalf("unexpected log record: %#v", record)
	}
	if record["status"] != float64(http.StatusAccepted) {
		t.Fatalf("status = %#v, want %d", record["status"], http.StatusAccepted)
	}
}

func TestRequestLoggerGeneratesRequestID(t *testing.T) {
	logger := slog.New(slog.NewJSONHandler(&bytes.Buffer{}, nil))
	handler := RequestLogger(logger, http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		if r.Header.Get(RequestIDHeader) == "" {
			t.Fatal("request ID was not generated")
		}
	}))
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	response := httptest.NewRecorder()

	handler.ServeHTTP(response, request)

	if response.Header().Get(RequestIDHeader) == "" {
		t.Fatal("response request ID was not set")
	}
}

func TestRequestLoggerSupportsFlush(t *testing.T) {
	logger := slog.New(slog.NewJSONHandler(&bytes.Buffer{}, nil))
	handler := RequestLogger(logger, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if err := http.NewResponseController(w).Flush(); err != nil {
			t.Fatalf("flush: %v", err)
		}
	}))
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	response := httptest.NewRecorder()

	handler.ServeHTTP(response, request)
}

func TestRequestLoggerSkipsTechnicalEndpoints(t *testing.T) {
	for _, path := range []string{"/healthz", "/metrics"} {
		t.Run(path, func(t *testing.T) {
			var output bytes.Buffer
			logger := slog.New(slog.NewJSONHandler(&output, nil))
			handler := RequestLogger(logger, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusOK)
			}))
			request := httptest.NewRequest(http.MethodGet, path, nil)
			response := httptest.NewRecorder()

			handler.ServeHTTP(response, request)

			if output.Len() != 0 {
				t.Fatalf("unexpected log: %s", output.String())
			}
		})
	}
}
