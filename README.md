# Inference server

An inference infrastructure repository combining a Go API gateway, llm-d standalone router,
vLLM workers, and local Kubernetes deployment profiles.

The gateway exposes OpenAI-compatible chat completions with Redis-backed rate
limiting. Optional profiles cover autoscaling and external KV caching with
LMCache. Prometheus and Grafana provide metrics, and Alloy forwards Pod logs
to persistent Loki storage.

## Quick start

Use the [local Kubernetes quickstart](docs/quickstart.md) to build and deploy
the mock pipeline. The [deployment guide](docs/deployment.md) describes the
ARM64 CPU profile and platform prerequisites.

## Repository

| Directory | Responsibility |
| --- | --- |
| `components/gateway/` | Gateway source, configuration, Dockerfile, and unit tests |
| `integrations/lmcache/` | Custom vLLM/LMCache image |
| `deploy/` | Kubernetes manifests, Helm values, and Docker Compose configuration |
| `tests/fixtures/mock-worker/` | Lightweight OpenAI-compatible test worker |
| `tests/e2e/` | Pipeline and experiment verification |
| `experiments/` | Reproducible procedures and interpretation limits |
| `docs/` | Architecture, deployment, development, and observability |

Benchmark source recovery is pending: `benchmarks/inference/` currently has no
canonical source implementation. Historical source snapshots and outputs remain
in the ignored `benchmark-results/` directory. Future local outputs belong under
the ignored `artifacts/` directory.

## Documentation

Read [architecture](docs/architecture.md) for request flow and state ownership,
then [deployment](docs/deployment.md) for the pinned llm-d Helm install and profiles, and
[observability](docs/observability.md) for debugging.

| Capability | Current scope |
| --- | --- |
| Routing | Redis admission; llm-d selects ready workers using queue and KV-utilization scores |
| Inference | Mock pipeline or single-node ARM64 CPU vLLM |
| External KV | One RAM-backed MP server; one- and two-worker profiles |
| Cross-worker reuse | Overlay and runner present; live validation pending |
| Autoscaling | Optional KEDA/HPA, one to three standard CPU workers |
| Debugging | Prometheus and Loki/Grafana; no distributed tracing |

P/D splitting, prefix-cache-aware scheduling, Redis-backed KV and custom hybrid
parallelism remain future work. Redis currently stores rate-limiter state only.

- [Architecture](docs/architecture.md)
- [Development and tests](docs/development.md)
- [Observability](docs/observability.md)
- [External KV-cache experiment](experiments/external-kv-cache/README.md)
- [Autoscaling experiment](experiments/autoscaling/README.md)
