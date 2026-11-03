package ratelimiter

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"strconv"
	"sync"
	"time"
)

const maxCapturedResponseBytes = 1 << 20

type ReservationLimiter interface {
	Reserve(context.Context, string, string, int64) (Reservation, error)
	Reconcile(context.Context, Reservation, int64) error
	Extend(context.Context, Reservation) error
}

type ScopeFunc func(*http.Request, string) string

type HTTPMiddleware struct {
	limiter          ReservationLimiter
	reservationTTL   time.Duration
	defaultMaxTokens int64
	scope            ScopeFunc
}

func NewHTTPMiddleware(limiter ReservationLimiter, reservationTTL time.Duration, defaultMaxTokens int64, scope ScopeFunc) (*HTTPMiddleware, error) {
	if limiter == nil || reservationTTL <= 0 || defaultMaxTokens <= 0 || scope == nil {
		return nil, errors.New("invalid rate limit middleware configuration")
	}
	return &HTTPMiddleware{
		limiter:          limiter,
		reservationTTL:   reservationTTL,
		defaultMaxTokens: defaultMaxTokens,
		scope:            scope,
	}, nil
}

func ModelScope(_ *http.Request, model string) string {
	return model
}

func (m *HTTPMiddleware) Wrap(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			writeAPIError(w, http.StatusBadRequest, "invalid request body", "invalid_request_error", "invalid_body")
			return
		}
		_ = r.Body.Close()
		r.Body = io.NopCloser(bytes.NewReader(body))

		request, err := parseChatCompletionRequest(body, m.defaultMaxTokens)
		if err != nil {
			writeAPIError(w, http.StatusBadRequest, err.Error(), "invalid_request_error", "invalid_request")
			return
		}

		requestID, err := newRequestID()
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "failed to create request ID", "server_error", "request_id_error")
			return
		}
		estimatedTokens := int64(len(body)) + request.maxTokens
		reservation, err := m.limiter.Reserve(r.Context(), m.scope(r, request.model), requestID, estimatedTokens)
		if err != nil {
			var limitError *LimitError
			if errors.As(err, &limitError) {
				w.Header().Set("Retry-After", retryAfterSeconds(limitError.RetryAfter))
				writeAPIError(w, http.StatusTooManyRequests, limitError.Error(), "rate_limit_error", limitError.Reason)
				return
			}
			log.Printf("reserve rate limit: %v", err)
			writeAPIError(w, http.StatusServiceUnavailable, "rate limiter unavailable", "server_error", "rate_limiter_unavailable")
			return
		}

		stopKeepAlive := m.keepAlive(reservation)
		actualTokens := estimatedTokens
		defer func() {
			stopKeepAlive()
			ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), 3*time.Second)
			defer cancel()
			if err := m.limiter.Reconcile(ctx, reservation, actualTokens); err != nil {
				log.Printf("reconcile rate limit reservation: %v", err)
			}
		}()

		capture := &responseCapture{ResponseWriter: w, enabled: !request.stream}
		next.ServeHTTP(capture, r)
		if usage, ok := responseTokenUsage(capture.body.Bytes()); ok {
			actualTokens = usage
		}
	})
}

func (m *HTTPMiddleware) keepAlive(reservation Reservation) func() {
	ctx, cancel := context.WithCancel(context.Background())
	var waitGroup sync.WaitGroup
	waitGroup.Add(1)
	go func() {
		defer waitGroup.Done()
		ticker := time.NewTicker(m.reservationTTL / 2)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				extendContext, extendCancel := context.WithTimeout(ctx, 3*time.Second)
				err := m.limiter.Extend(extendContext, reservation)
				extendCancel()
				if err != nil {
					log.Printf("extend rate limit reservation: %v", err)
					return
				}
			case <-ctx.Done():
				return
			}
		}
	}()

	return func() {
		cancel()
		waitGroup.Wait()
	}
}

type chatCompletionRequest struct {
	Model               string `json:"model"`
	MaxTokens           *int64 `json:"max_tokens"`
	MaxCompletionTokens *int64 `json:"max_completion_tokens"`
	Stream              bool   `json:"stream"`
}

type parsedChatCompletionRequest struct {
	model     string
	maxTokens int64
	stream    bool
}

func parseChatCompletionRequest(body []byte, defaultMaxTokens int64) (parsedChatCompletionRequest, error) {
	var request chatCompletionRequest
	if err := json.Unmarshal(body, &request); err != nil {
		return parsedChatCompletionRequest{}, errors.New("request body must be valid JSON")
	}
	if request.Model == "" {
		return parsedChatCompletionRequest{}, errors.New("model is required")
	}

	maxTokens := defaultMaxTokens
	if request.MaxTokens != nil {
		maxTokens = *request.MaxTokens
	}
	if request.MaxCompletionTokens != nil {
		maxTokens = *request.MaxCompletionTokens
	}
	if maxTokens < 0 {
		return parsedChatCompletionRequest{}, errors.New("max tokens cannot be negative")
	}

	return parsedChatCompletionRequest{model: request.Model, maxTokens: maxTokens, stream: request.Stream}, nil
}

type responseCapture struct {
	http.ResponseWriter
	body    bytes.Buffer
	enabled bool
}

func (w *responseCapture) Write(data []byte) (int, error) {
	if w.enabled {
		if w.body.Len()+len(data) <= maxCapturedResponseBytes {
			_, _ = w.body.Write(data)
		} else {
			w.enabled = false
			w.body.Reset()
		}
	}
	return w.ResponseWriter.Write(data)
}

func (w *responseCapture) Unwrap() http.ResponseWriter {
	return w.ResponseWriter
}

func (w *responseCapture) Flush() {
	_ = http.NewResponseController(w.ResponseWriter).Flush()
}

func responseTokenUsage(body []byte) (int64, bool) {
	var response struct {
		Usage *struct {
			PromptTokens     int64 `json:"prompt_tokens"`
			CompletionTokens int64 `json:"completion_tokens"`
			TotalTokens      int64 `json:"total_tokens"`
		} `json:"usage"`
	}
	if len(body) == 0 || json.Unmarshal(body, &response) != nil || response.Usage == nil {
		return 0, false
	}
	if response.Usage.TotalTokens > 0 {
		return response.Usage.TotalTokens, true
	}
	return response.Usage.PromptTokens + response.Usage.CompletionTokens, true
}

func newRequestID() (string, error) {
	data := make([]byte, 16)
	if _, err := rand.Read(data); err != nil {
		return "", err
	}
	return hex.EncodeToString(data), nil
}

func retryAfterSeconds(duration time.Duration) string {
	seconds := int64((duration + time.Second - 1) / time.Second)
	if seconds < 1 {
		seconds = 1
	}
	return strconv.FormatInt(seconds, 10)
}

func writeAPIError(w http.ResponseWriter, status int, message, errorType, code string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"error": map[string]string{
			"message": message,
			"type":    errorType,
			"code":    code,
		},
	})
}
