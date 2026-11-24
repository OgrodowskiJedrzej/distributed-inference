# Development

Run commands from the repository root unless a command changes directory.

## Gateway

The gateway loads the file specified by `CONFIG_PATH`, falling back to
`config.yaml` in its working directory. The image contains no configuration;
Kubernetes mounts it from a ConfigMap. For local development:

```sh
cd components/gateway
go test ./...
CONFIG_PATH=../../deploy/kubernetes/base/gateway/config.yaml go run ./cmd
```

Redis must be available at the configured address. Environment overrides include
`UPSTREAM_URL`, `REDIS_ADDRESS`, and `REDIS_PASSWORD`.
The Redis stress test additionally requires `REDIS_TEST_ADDRESS` and optionally
`REDIS_TEST_PASSWORD`; it is skipped when the address is absent.

## Docker Compose worker

```sh
docker compose -f deploy/compose/compose.yaml up -d
```

This ARM64 profile starts only a real CPU worker on port 8001 and serves
`test-model`. The base gateway configuration allows `mock-qwen`, so
the Compose profile requires a matching gateway model configuration and a
separately running Redis. The Kubernetes quickstart provides the matching
mock pipeline.

## End-to-end tests

Requires Docker, kind, kubectl, Helm, curl and Python 3; Gateway development also
requires Go. Kubernetes procedures target `kind-inference-lab`.

| Entry point | Purpose | Side effects |
| --- | --- | --- |
| Gateway `go test ./...` | Unit tests; Redis stress test opt-in | Stress test uses configured Redis |
| `tests/e2e/pipeline.sh` | Pipeline smoke and mock log correlation | Builds images, applies manifests and Helm releases |
| `tests/e2e/autoscaling.py` | Scaling and worker participation | Load and replica changes |
| `tests/e2e/external_kv_cache.py` | Reuse after worker replacement | Restarts worker Deployment |
| `tests/e2e/shared_kv_cache.py` | Cross-worker reuse and replacement | Deletes one worker Pod |

Cache runners require new output directories, preserve evidence and stop
port-forwards without removing deployments. Read the
[cache procedure](../experiments/external-kv-cache/README.md) first: these are
not passive health checks. Live multi-worker validation remains pending.

Run the end-to-end smoke test against the deployed stack:

```sh
./tests/e2e/pipeline.sh
```

The test checks the three Kubernetes rollouts, opens a temporary port-forward,
and validates chat completion responses passing through the gateway, router,
and workers. To additionally require mock traffic to reach at least two workers:

```sh
E2E_REQUESTS=10 E2E_MIN_WORKERS=2 ./tests/e2e/pipeline.sh
```

The pipeline builds images and applies manifests and Helm releases to the
selected kind cluster. Redis and its credentials must already exist.
Run the [autoscaling procedure](../experiments/autoscaling/README.md) first
if a worker ScaledObject is active.

Benchmark source snapshots are retained with historical outputs. Selecting a
canonical implementation for `benchmarks/inference/` is a separate task.
