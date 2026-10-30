package api

import (
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestChatCompletionsProxiesRequest(t *testing.T) {
	worker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method = %q, want %q", r.Method, http.MethodPost)
		}
		if r.URL.Path != "/v1/chat/completions" {
			t.Errorf("path = %q, want %q", r.URL.Path, "/v1/chat/completions")
		}

		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("read request body: %v", err)
			return
		}
		if string(body) != `{"model":"test-model"}` {
			t.Errorf("body = %q, want request body to be preserved", body)
		}

		w.Write([]byte("worker-response"))
	}))
	defer worker.Close()

	upstream, err := url.Parse(worker.URL)
	if err != nil {
		t.Fatal(err)
	}

	handler := NewChatCompletionsHandler(upstream)
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"test-model"}`))
	response := httptest.NewRecorder()

	handler.ServeHTTP(response, request)

	body, err := io.ReadAll(response.Result().Body)
	if err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusOK)
	}
	if string(body) != "worker-response" {
		t.Fatalf("body = %q, want %q", body, "worker-response")
	}
}
