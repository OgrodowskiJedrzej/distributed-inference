package observability

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

func TestInstrumentRecordsRequestAndPreservesFlushing(t *testing.T) {
	registry := prometheus.NewRegistry()
	metrics := NewHTTPMetrics(registry)
	handler := metrics.Instrument("/test", http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if _, ok := w.(http.Flusher); !ok {
			t.Fatal("instrumented response writer does not implement http.Flusher")
		}
		w.WriteHeader(http.StatusAccepted)
	}))

	request := httptest.NewRequest(http.MethodPost, "/test", nil)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)

	if response.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusAccepted)
	}
	if got := testutil.ToFloat64(metrics.requests.WithLabelValues("202", "post", "/test")); got != 1 {
		t.Fatalf("request count = %v, want 1", got)
	}
	if got := testutil.ToFloat64(metrics.inFlight.WithLabelValues("/test")); got != 0 {
		t.Fatalf("in-flight requests = %v, want 0", got)
	}
}
