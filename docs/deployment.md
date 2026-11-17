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

| Path under `deploy/kubernetes/` | Workers | Runtime KV |
| --- | --- | --- |
| `base` | 3 mock | No real model KV |
| `overlays/local-vllm-cpu` | 1 real CPU | Worker-local |
| `overlays/local-vllm-kv-cache` | 1 real CPU | Shared MP RAM |
| `overlays/local-vllm-kv-cache-multi` | 2 real CPU | The same single MP server |

Profiles update the same Deployments, not separate installations. Applying an
overlay does not prune earlier resources; follow experiment rollback procedures.
Autoscaling is applied separately and controls replicas through an HPA.

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

Model revision: `12fd25f77366fa6b3b4b768ec3050bf629380bac`. Workers use float32,
a 1024-token context, at most two concurrent sequences and a 128 MiB vLLM KV
budget. Each requests 2 CPU / 4 GiB and is limited to 4 CPU / 6 GiB. Startup
checks run an actual one-token generation; readiness uses `/health`.

Model files use a 10 GiB ReadWriteOnce PVC. Each Pod has a separate 256 MiB
memory-backed `/dev/shm`. External-cache profiles add an MP server requesting
1 CPU / 2 GiB, limited to 2 CPU / 4 GiB. Its 1 GiB L1 is not total process
memory. Allow additional node capacity for monitoring and system workloads.

The model PVC does not preserve runtime KV. MP has no KV PVC; restart clears
its cache. Loki has a separate log PVC; Prometheus has none configured.
See [state ownership](architecture.md#state-ownership).

Do not apply fixed-replica profiles while the worker ScaledObject is active.
Follow the rollback procedure in the
[autoscaling experiment](../experiments/autoscaling/README.md).

## Optional profiles

- [External KV cache](../experiments/external-kv-cache/README.md)
- [Autoscaling](../experiments/autoscaling/README.md)
- [Metrics and logs](observability.md)

The external-cache experiment provides two overlays:

| Overlay | Workers | Shared LMCache MP servers |
| --- | --- | --- |
| `deploy/kubernetes/overlays/local-vllm-kv-cache` | 1 | 1 |
| `deploy/kubernetes/overlays/local-vllm-kv-cache-multi` | 2 | 1 |

Both use the same model, image, connector, and RAM-backed cache. The multi-worker
overlay inherits the single-worker overlay and patches only `spec.replicas`.
Use the [experiment instructions](../experiments/external-kv-cache/README.md#two-worker-sharing)
to run the test, which intentionally replaces one worker Pod. Autoscaling must
be disabled. The profiles currently target one ARM64 node with a shared
ReadWriteOnce model-file PVC; they do not provide multi-node model storage.

The base and existing overlay names are preserved during repository migration.
`deploy/kubernetes/components/autoscaling/` is an independently applied
Kustomization containing the ScaledObject, not a Kustomize `Component` resource.
