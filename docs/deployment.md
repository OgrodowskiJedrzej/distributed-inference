# Deployment

Run commands from the repository root. The Kubernetes profiles use the
`inference` namespace and expect Redis at `inference-redis-master:6379`.
The `redis-auth` Secret must contain a `redis-password` key.

Platform Helm values are kept in `deploy/platform/redis/` and
`deploy/platform/keda/`. Redis is installed separately; applying the application
Kustomize base does not install it or create its credentials. The Redis values
enable a ServiceMonitor, so the Prometheus Operator CRDs must exist before
installing that configuration.

## Mock pipeline

Build the local images and load them into the kind cluster. Kubernetes pulls
the pinned vLLM Router image from Docker Hub when the router is deployed.

```sh
docker build -t mock-vllm:v0.1 tests/fixtures/mock-worker
docker build -t inference-gateway:v0.1 components/gateway
kind load docker-image mock-vllm:v0.1 inference-gateway:v0.1 --name inference-lab
```

Deploy the workers, vLLM Router, and gateway:

```sh
kubectl apply -k deploy/kubernetes/base
kubectl rollout status deployment/mock-vllm -n inference
kubectl rollout status deployment/vllm-router -n inference
kubectl rollout status deployment/inference-gateway -n inference
```

For a complete request example, see the [quickstart](quickstart.md).

## ARM64 CPU profile

On an ARM64 development cluster, deploy a real CPU-backed vLLM worker instead
of the mock workers:

```sh
kubectl apply -k deploy/kubernetes/overlays/local-vllm-cpu
kubectl rollout status deployment/mock-vllm -n inference --timeout=15m
```

This profile runs one `HuggingFaceTB/SmolLM2-135M-Instruct` instance and preserves the
`mock-qwen` API model name. The first start downloads the model into a persistent
cache and can take several minutes.

Do not apply fixed-replica profiles while the worker ScaledObject is active.
Follow the rollback procedure in the
[autoscaling experiment](../experiments/autoscaling/README.md).

## Optional profiles

- [External KV cache](../experiments/external-kv-cache/README.md)
- [Autoscaling](../experiments/autoscaling/README.md)
- [Metrics and logs](observability.md)

The base and existing overlay names are preserved during repository migration.
`deploy/kubernetes/components/autoscaling/` is an independently applied
Kustomization containing the ScaledObject, not a Kustomize `Component` resource.
