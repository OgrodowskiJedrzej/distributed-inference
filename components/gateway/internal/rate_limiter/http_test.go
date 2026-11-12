package ratelimiter

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

type fakeReservationLimiter struct {
	reserveError      error
	reserveCalls      int
	reservedScope     string
	reservedRequestID string
	reservedTokens    int64
	reconciledTokens  int64
	extendCalls       int
}

func (l *fakeReservationLimiter) Reserve(_ context.Context, scope, requestID string, tokens int64) (Reservation, error) {
	l.reserveCalls++
	if l.reserveError != nil {
		return Reservation{}, l.reserveError
	}
	l.reservedScope = scope
	l.reservedRequestID = requestID
	l.reservedTokens = tokens
	return Reservation{Scope: scope, ID: requestID, ReservedTokens: tokens}, nil
}

func TestHTTPMiddlewareRejectsInvalidModelPolicyRequests(t *testing.T) {
	tests := []struct {
		name      string
		body      string
		errorCode string
	}{
		{name: "model not allowed", body: `{"model":"blocked"}`, errorCode: "model_not_allowed"},
		{name: "max tokens exceeds limit", body: `{"model":"allowed","max_tokens":11}`, errorCode: "max_tokens_exceeded"},
		{name: "both token fields", body: `{"model":"allowed","max_tokens":10,"max_completion_tokens":9}`, errorCode: "ambiguous_max_tokens"},
		{name: "zero max tokens", body: `{"model":"allowed","max_tokens":0}`, errorCode: "max_tokens_exceeded"},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			limiter := &fakeReservationLimiter{}
			middleware, err := NewHTTPMiddleware(limiter, 10*time.Minute, ModelScope, map[string]int64{"allowed": 10})
			if err != nil {
				t.Fatal(err)
			}

			called := false
			handler := middleware.Wrap(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
				called = true
			}))
			request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(test.body))
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, request)

			if called {
				t.Fatal("upstream handler was called")
			}
			if limiter.reserveCalls != 0 {
				t.Fatalf("reserve calls = %d, want 0", limiter.reserveCalls)
			}
			if response.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want %d", response.Code, http.StatusBadRequest)
			}
			var payload struct {
				Error struct {
					Code string `json:"code"`
				} `json:"error"`
			}
			if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
				t.Fatal(err)
			}
			if payload.Error.Code != test.errorCode {
				t.Fatalf("error code = %q, want %q", payload.Error.Code, test.errorCode)
			}
		})
	}
}

func TestHTTPMiddlewareUsesModelMaxTokensByDefault(t *testing.T) {
	limiter := &fakeReservationLimiter{}
	middleware, err := NewHTTPMiddleware(limiter, 10*time.Minute, ModelScope, map[string]int64{"allowed": 10})
	if err != nil {
		t.Fatal(err)
	}

	requestBody := `{"model":"allowed"}`
	handler := middleware.Wrap(http.HandlerFunc(func(_ http.ResponseWriter, request *http.Request) {
		body, err := io.ReadAll(request.Body)
		if err != nil {
			t.Fatal(err)
		}
		var payload struct {
			MaxTokens int64 `json:"max_tokens"`
		}
		if err := json.Unmarshal(body, &payload); err != nil {
			t.Fatal(err)
		}
		if payload.MaxTokens != 10 {
			t.Fatalf("max tokens = %d, want 10", payload.MaxTokens)
		}
	}))
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(requestBody))
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusOK)
	}
	if limiter.reservedTokens != int64(len(`{"max_tokens":10,"model":"allowed"}`))+10 {
		t.Fatalf("reserved tokens = %d, want %d", limiter.reservedTokens, len(`{"max_tokens":10,"model":"allowed"}`)+10)
	}
}

func (l *fakeReservationLimiter) Reconcile(_ context.Context, _ Reservation, tokens int64) error {
	l.reconciledTokens = tokens
	return nil
}

func (l *fakeReservationLimiter) Extend(_ context.Context, _ Reservation) error {
	l.extendCalls++
	return nil
}

func TestHTTPMiddlewareReservesAndReconcilesUsage(t *testing.T) {
	limiter := &fakeReservationLimiter{}
	middleware, err := NewHTTPMiddleware(limiter, 10*time.Minute, func(_ *http.Request, model string) string {
		return "tenant-a:" + model
	}, map[string]int64{"test-model": 512})
	if err != nil {
		t.Fatal(err)
	}

	requestBody := `{"model":"test-model","messages":[],"max_tokens":10}`
	handler := middleware.Wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatal(err)
		}
		if string(body) != requestBody {
			t.Fatalf("body = %q, want %q", body, requestBody)
		}
		if _, ok := w.(http.Flusher); !ok {
			t.Fatal("wrapped response writer does not implement http.Flusher")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}`))
	}))

	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(requestBody))
	request.Header.Set("X-Request-ID", "request-123")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusOK)
	}
	if limiter.reservedScope != "tenant-a:test-model" {
		t.Fatalf("scope = %q, want tenant-a:test-model", limiter.reservedScope)
	}
	if limiter.reservedRequestID == "" || limiter.reservedRequestID == "request-123" {
		t.Fatalf("reservation ID = %q, want a generated internal ID", limiter.reservedRequestID)
	}
	if limiter.reservedTokens != int64(len(requestBody))+10 {
		t.Fatalf("reserved tokens = %d, want %d", limiter.reservedTokens, len(requestBody)+10)
	}
	if limiter.reconciledTokens != 7 {
		t.Fatalf("reconciled tokens = %d, want 7", limiter.reconciledTokens)
	}
}

func TestHTTPMiddlewareReturnsRateLimitError(t *testing.T) {
	limiter := &fakeReservationLimiter{
		reserveError: &LimitError{Reason: "tokens", RetryAfter: 1500 * time.Millisecond},
	}
	middleware, err := NewHTTPMiddleware(limiter, 10*time.Minute, ModelScope, map[string]int64{"test-model": 512})
	if err != nil {
		t.Fatal(err)
	}

	called := false
	handler := middleware.Wrap(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		called = true
	}))
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"test-model"}`))
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)

	if called {
		t.Fatal("upstream handler was called")
	}
	if response.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusTooManyRequests)
	}
	if response.Header().Get("Retry-After") != "2" {
		t.Fatalf("Retry-After = %q, want 2", response.Header().Get("Retry-After"))
	}
}

func TestHTTPMiddlewareReturnsServiceUnavailableForRedisError(t *testing.T) {
	limiter := &fakeReservationLimiter{reserveError: errors.New("redis unavailable")}
	middleware, err := NewHTTPMiddleware(limiter, 10*time.Minute, ModelScope, map[string]int64{"test-model": 512})
	if err != nil {
		t.Fatal(err)
	}

	handler := middleware.Wrap(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Fatal("upstream handler was called")
	}))
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"test-model"}`))
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)

	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusServiceUnavailable)
	}
}

func TestHTTPMiddlewareRejectsOversizedRequestBody(t *testing.T) {
	limiter := &fakeReservationLimiter{}
	middleware, err := NewHTTPMiddleware(limiter, 10*time.Minute, ModelScope, map[string]int64{"test-model": 512})
	if err != nil {
		t.Fatal(err)
	}

	called := false
	handler := middleware.Wrap(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		called = true
	}))
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	request.Body = io.NopCloser(strings.NewReader(strings.Repeat("a", maxRequestBodySize+1)))
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)

	if called {
		t.Fatal("upstream handler was called")
	}
	if response.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusRequestEntityTooLarge)
	}
	var payload struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}
	if payload.Error.Code != "request_too_large" {
		t.Fatalf("error code = %q, want request_too_large", payload.Error.Code)
	}
	if limiter.reservedTokens != 0 {
		t.Fatalf("reserved tokens = %d, want 0", limiter.reservedTokens)
	}
}

func TestHTTPMiddlewareChargesReservationForStreamingResponse(t *testing.T) {
	limiter := &fakeReservationLimiter{}
	middleware, err := NewHTTPMiddleware(limiter, 10*time.Minute, ModelScope, map[string]int64{"test-model": 20})
	if err != nil {
		t.Fatal(err)
	}

	requestBody := `{"model":"test-model","stream":true}`
	handler := middleware.Wrap(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("data: chunk\n\n"))
		w.(http.Flusher).Flush()
	}))
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(requestBody))
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)

	want := int64(len(`{"max_tokens":20,"model":"test-model","stream":true}`) + 20)
	if limiter.reconciledTokens != want {
		t.Fatalf("reconciled tokens = %d, want %d", limiter.reconciledTokens, want)
	}
}
