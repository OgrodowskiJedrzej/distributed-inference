# Development

Run commands from the repository root unless a command changes directory.

## Gateway

The gateway reads `config.yaml` relative to its working directory:

```sh
cd components/gateway
go test ./...
go run ./cmd
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
`test-model`. The gateway's default configuration allows `mock-qwen`, so
the Compose profile requires a matching gateway model configuration and a
separately running Redis. The Kubernetes quickstart provides the matching
mock pipeline.

## End-to-end tests

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
