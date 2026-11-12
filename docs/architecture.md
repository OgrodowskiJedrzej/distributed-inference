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

The external KV-cache profile uses a custom image from
`integrations/lmcache/` and a separate in-memory LMCache server. It validates
reuse across worker replacement with one worker; it does not implement
prefill/decode splitting.

Prometheus scrapes gateway, router, and worker metrics. KEDA uses worker load
metrics for optional autoscaling. Alloy collects Pod logs from `inference`
and forwards them to Loki; Grafana provides access to metrics and logs.

Application manifests live in `deploy/kubernetes/`, platform dependencies
in `deploy/platform/`, and monitoring configuration in
`deploy/observability/`. Experiment procedures reference these shared
configurations and keep their validation scripts in `tests/e2e/`.
