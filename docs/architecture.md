# Architecture

The request path is:

```text
Client -> Gateway -> vLLM Router -> Worker
             |
             +-> Redis rate limiter

Worker -> LMCache server (optional external KV-cache profile)
```

The Go gateway exposes `POST /v1/chat/completions`, validates the model policy,
reserves rate-limit capacity in Redis, and proxies the request to vLLM Router.
Request correlation IDs and internal Redis reservation IDs are independent.

vLLM Router is an external image. Kubernetes service discovery finds workers,
and the configured `power_of_two` policy distributes requests among them.

The base profile uses the mock worker from `tests/fixtures/mock-worker/`.
The ARM64 CPU overlay replaces it with vLLM serving
`HuggingFaceTB/SmolLM2-135M-Instruct` as `mock-qwen`. Kubernetes resource
names retain `mock-vllm` across profiles.

The external KV-cache profiles use a custom image from
`integrations/lmcache/` and a separate in-memory LMCache MP server. The
single-worker profile tests reuse after replacement. The multi-worker profile
connects two identical vLLM replicas to that same server and provides a test for
cross-worker reuse. Each replica performs both prefill and decode.

The router retains `power_of_two`; worker selection does not require session
affinity because both replicas can retrieve the same externally cached prefix.
KV lookup follows the input token prefix and compatible model/cache identity.
Clients must still provide the full conversation in each chat request. Missing
cache blocks can be recomputed; KV storage is not a conversation database.
Only the external KV-cache profiles explicitly disable local prefix caching to
isolate external hits; the standard CPU profile does not set that flag.

The multi-worker profile is initially limited to one ARM64 node and one shared
RAM cache with LRU eviction. MP restart recovery, cache-aware routing, and
cross-node model-file storage are separate validation tasks. See the
[experiment procedures](../experiments/external-kv-cache/README.md).

Prometheus scrapes gateway, router, and worker metrics. KEDA uses worker load
metrics for optional autoscaling. Alloy collects Pod logs from `inference`
and forwards them to Loki; Grafana provides access to metrics and logs.

Application manifests live in `deploy/kubernetes/`, platform dependencies
in `deploy/platform/`, and monitoring configuration in
`deploy/observability/`. Experiment procedures reference these shared
configurations and keep their validation scripts in `tests/e2e/`.

## Request lifecycle

1. Client sends the full chat history to Gateway. Gateway generates a new
   `X-Request-ID`, overwriting any supplied ID and returning it in the response.
2. Gateway validates the model and output limit and reserves Redis capacity.
   Limits are per model, not per user. Exhaustion returns 429; Redis failure 503.
3. Router discovers healthy worker endpoints and selects with `power_of_two`.
   No conversation affinity or cache-aware selection is configured.
4. Worker performs prefill and decode. With external KV, compatible token-prefix
   blocks can be retrieved from MP. Missing or evicted blocks are recomputed;
   transport-error fallback has not been validated.
5. Gateway forwards the response, reconciles the reservation from response usage
   when available, and logs completion.

Token reservation estimates request-body bytes plus the output limit, not
tokenizer counts. SSE usage is not parsed; streaming and missing, oversized or
unreadable usage retain the estimate. Request IDs are not cache keys.

## State ownership

| State | Owner | Lifetime |
| --- | --- | --- |
| Chat history | Client | Resent each request; no conversation database |
| Admission buckets and leases | Redis | Shared per-model limits, not model KV |
| Active generation and local KV | Worker | Process-local |
| External prefix KV | Single MP server | 1 GiB RAM LRU; lost on server restart |
| Model files | Model-cache PVC | Persistent downloads, not runtime KV |
| Logs | Loki PVC | Seven-day retention configured, capacity-dependent |
| Log positions | Alloy node hostPath | Survive replacement on the same node |
| Metrics | Prometheus | Six-hour retention; no PVC configured |

Workers require compatible model revision, tokenizer, dtype and KV layout.
Two independent MP replicas behind a Service would not create a coherent L1.
Concurrent cold requests can compute the same prefix; single-flight is not
guaranteed. Worker replacement can reuse surviving KV, but cannot be assumed
to resume an interrupted generation. Retry is not live migration.

## Implementation boundaries

Gateway is repository Go code; Router is `vllm/vllm-router:v0.1.16`. The mock
worker is a Python fixture. Real CPU profiles use vLLM `0.31.0-arm64`; the
integration image adds pinned LMCache. MP uses TCP 5555 and management port 8080.

Gateway exposes only chat completions, `/healthz` and `/metrics`. Health does
not check dependencies. Tests access `/v1/completions`, `/tokenize` and
`/v1/models` directly on Router/workers, not through Gateway.

The multi-worker overlay and runner exist; live cross-worker validation is
pending. P/D splitting, cache-aware scheduling, Redis-backed model KV and custom
hybrid parallelism are not implemented. vLLM already schedules batches internally
with at most two concurrent sequences in CPU profiles. No custom batch scheduler
or distributed tracing is configured.
