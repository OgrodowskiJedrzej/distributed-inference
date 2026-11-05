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

const maxCapturedResponseBytes = 1 << 20 // 1 MiB
const maxRequestBodySize = 1 << 20       // 1 MiB

type ReservationLimiter interface {
	Reserve(context.Context, string, string, int64) (Reservation, error)
	Reconcile(context.Context, Reservation, int64) error
	Extend(context.Context, Reservation) error
}

type ScopeFunc func(*http.Request, string) string

type HTTPMiddleware struct {
	limiter        ReservationLimiter
	reservationTTL time.Duration
	modelMaxTokens map[string]int64
	scope          ScopeFunc
}

func NewHTTPMiddleware(limiter ReservationLimiter, reservationTTL time.Duration, scope ScopeFunc, modelMaxTokens map[string]int64) (*HTTPMiddleware, error) {
	if limiter == nil || reservationTTL <= 0 || scope == nil || len(modelMaxTokens) == 0 {
		return nil, errors.New("invalid rate limit middleware configuration")
	}
	for model, maxTokens := range modelMaxTokens {
		if model == "" || maxTokens <= 0 {
			return nil, errors.New("invalid model policy")
		}
	}
	return &HTTPMiddleware{
		limiter:        limiter,
		reservationTTL: reservationTTL,
		modelMaxTokens: modelMaxTokens,
		scope:          scope,
	}, nil
}

func ModelScope(_ *http.Request, model string) string {
	return model
}

func (m *HTTPMiddleware) Wrap(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodySize)
		body, err := io.ReadAll(r.Body)
		if err != nil {
			var maxBytesError *http.MaxBytesError
			if errors.As(err, &maxBytesError) {
				writeAPIError(w, http.StatusRequestEntityTooLarge, "request body too large", "invalid_request_error", "request_too_large")
				return
			}
			writeAPIError(w, http.StatusBadRequest, "invalid request body", "invalid_request_error", "invalid_body")
			return
		}
		_ = r.Body.Close()
		r.Body = io.NopCloser(bytes.NewReader(body))

		request, err := parseChatCompletionRequest(body)
		if err != nil {
			code := "invalid_request"
			var validationError *requestValidationError
			if errors.As(err, &validationError) {
				code = validationError.code
			}
			writeAPIError(w, http.StatusBadRequest, err.Error(), "invalid_request_error", code)
			return
		}
		maxTokens, allowed := m.modelMaxTokens[request.model]
		if !allowed {
			writeAPIError(w, http.StatusBadRequest, "model is not allowed", "invalid_request_error", "model_not_allowed")
			return
		}
		if !request.maxTokensProvided {
			request.maxTokens = maxTokens
			body, err = withMaxTokens(body, maxTokens)
			if err != nil {
				log.Printf("add default max tokens: %v", err)
				writeAPIError(w, http.StatusInternalServerError, "failed to prepare request", "server_error", "request_rewrite_error")
				return
			}
			r.Body = io.NopCloser(bytes.NewReader(body))
			r.ContentLength = int64(len(body))
			r.TransferEncoding = nil
		} else if request.maxTokens > maxTokens {
			writeAPIError(w, http.StatusBadRequest, "max tokens exceeds model limit", "invalid_request_error", "max_tokens_exceeded")
			return
		}

		if request.maxTokens <= 0 {
			writeAPIError(w, http.StatusBadRequest, "max tokens must be positive", "invalid_request_error", "max_tokens_exceeded")
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
	model             string
	maxTokens         int64
	maxTokensProvided bool
	stream            bool
}

type requestValidationError struct {
	message string
	code    string
}

func (e *requestValidationError) Error() string {
	return e.message
}

func parseChatCompletionRequest(body []byte) (parsedChatCompletionRequest, error) {
	var request chatCompletionRequest
	if err := json.Unmarshal(body, &request); err != nil {
		return parsedChatCompletionRequest{}, &requestValidationError{message: "request body must be valid JSON", code: "invalid_request"}
	}
	if request.Model == "" {
		return parsedChatCompletionRequest{}, &requestValidationError{message: "model is required", code: "invalid_request"}
	}
	if request.MaxTokens != nil && request.MaxCompletionTokens != nil {
		return parsedChatCompletionRequest{}, &requestValidationError{message: "only one of max_tokens and max_completion_tokens may be set", code: "ambiguous_max_tokens"}
	}

	var maxTokens int64
	maxTokensProvided := false
	if request.MaxTokens != nil {
		maxTokens = *request.MaxTokens
		maxTokensProvided = true
	} else if request.MaxCompletionTokens != nil {
		maxTokens = *request.MaxCompletionTokens
		maxTokensProvided = true
	}
	if maxTokensProvided && maxTokens <= 0 {
		return parsedChatCompletionRequest{}, &requestValidationError{message: "max tokens must be positive", code: "max_tokens_exceeded"}
	}

	return parsedChatCompletionRequest{model: request.Model, maxTokens: maxTokens, maxTokensProvided: maxTokensProvided, stream: request.Stream}, nil
}

func withMaxTokens(body []byte, maxTokens int64) ([]byte, error) {
	var request map[string]json.RawMessage
	if err := json.Unmarshal(body, &request); err != nil {
		return nil, err
	}
	request["max_tokens"] = json.RawMessage(strconv.FormatInt(maxTokens, 10))
	return json.Marshal(request)
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
