package observability

import (
	"net/http"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

type HTTPMetrics struct {
	requests *prometheus.CounterVec
	duration *prometheus.HistogramVec
	inFlight *prometheus.GaugeVec
}

func NewHTTPMetrics(registerer prometheus.Registerer) *HTTPMetrics {
	requests := prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Namespace: "gateway",
			Subsystem: "http",
			Name:      "requests_total",
			Help:      "Total number of HTTP requests.",
		},
		[]string{"code", "method", "route"},
	)
	duration := prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Namespace: "gateway",
			Subsystem: "http",
			Name:      "request_duration_seconds",
			Help:      "HTTP request duration in seconds.",
			Buckets:   []float64{0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300},
		},
		[]string{"method", "route"},
	)
	inFlight := prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Namespace: "gateway",
			Subsystem: "http",
			Name:      "requests_in_flight",
			Help:      "Current number of HTTP requests being served.",
		},
		[]string{"route"},
	)

	registerer.MustRegister(requests, duration, inFlight)

	return &HTTPMetrics{
		requests: requests,
		duration: duration,
		inFlight: inFlight,
	}
}

func (m *HTTPMetrics) Instrument(route string, handler http.Handler) http.Handler {
	labels := prometheus.Labels{"route": route}
	handler = promhttp.InstrumentHandlerCounter(m.requests.MustCurryWith(labels), handler)
	handler = promhttp.InstrumentHandlerDuration(m.duration.MustCurryWith(labels), handler)
	return promhttp.InstrumentHandlerInFlight(m.inFlight.WithLabelValues(route), handler)
}
