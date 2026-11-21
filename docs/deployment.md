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

Build the local images and load them into the kind cluster. Helm pulls the
pinned llm-d chart and its versioned EPP/Envoy images separately.

```sh
docker build -t mock-vllm:v0.1 tests/fixtures/mock-worker
docker build -t inference-gateway:v0.1 components/gateway
kind load docker-image mock-vllm:v0.1 inference-gateway:v0.1 --name inference-lab
```

Deploy llm-d, workers and Gateway (Helm and kubectl are required):

```sh
kubectl --context kind-inference-lab apply -f deploy/kubernetes/base/namespace.yaml
kubectl --context kind-inference-lab apply -f \
  https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/v1.6.2/v1-manifests.yaml
kubectl --context kind-inference-lab wait --for=condition=Established \
  crd/inferencepools.inference.networking.k8s.io --timeout=60s
helm upgrade --install llmd \
  oci://ghcr.io/llm-d/charts/llm-d-router-standalone@sha256:c1d1af55382ef1097188d188070385c6de02b771f34681678e658a59e975024c \
  --kube-context kind-inference-lab --namespace inference \
  --values deploy/llm-d/values.yaml --wait --timeout 5m
kubectl apply -k deploy/kubernetes/base
kubectl rollout status deployment/mock-vllm -n inference
kubectl rollout status deployment/llmd-epp -n inference
kubectl rollout status deployment/inference-gateway -n inference
```

For a complete request example, see the [quickstart](quickstart.md).

## llm-d lifecycle and migration

The fixed release llmd in inference uses the upstream standalone chart v1.0.0,
pinned by OCI digest, with Gateway API Inference Extension CRDs v1.6.2.
No Gateway API controller, Ray, P/D sidecar or NIXL is installed. Envoy exposes
llmd-epp:8081; EPP metrics use the http-metrics Service port (9090).
deploy/llm-d/values.yaml owns scheduling and development resources. Prefix
affinity is deliberately disabled; routing scores queue depth and KV utilization.
Mock workers have no vLLM metrics and cannot demonstrate load-aware behavior.

Render without contacting the Kubernetes API or changing a cluster:

```sh
helm template llmd \
  oci://ghcr.io/llm-d/charts/llm-d-router-standalone@sha256:c1d1af55382ef1097188d188070385c6de02b771f34681678e658a59e975024c \
  --namespace inference --values deploy/llm-d/values.yaml
kubectl kustomize deploy/kubernetes/base
```

The commands below use an explicit context. They apply the namespace and
cluster-scoped CRDs, then upgrade/install Helm with --wait. Review existing
CRD consumers before upgrading a shared cluster. They do not deploy workers
or Gateway. The Helm commands support Helm 3 and Helm 4.

```sh
kubectl --context YOUR_KUBERNETES_CONTEXT apply -f deploy/kubernetes/base/namespace.yaml
kubectl --context YOUR_KUBERNETES_CONTEXT apply -f \
  https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/v1.6.2/v1-manifests.yaml
kubectl --context YOUR_KUBERNETES_CONTEXT wait --for=condition=Established \
  crd/inferencepools.inference.networking.k8s.io --timeout=60s
helm upgrade --install llmd \
  oci://ghcr.io/llm-d/charts/llm-d-router-standalone@sha256:c1d1af55382ef1097188d188070385c6de02b771f34681678e658a59e975024c \
  --kube-context YOUR_KUBERNETES_CONTEXT --namespace inference \
  --values deploy/llm-d/values.yaml --wait --timeout 5m
```

Stop clients before cutover. Install llm-d, apply the selected application
profile, wait for workers and Gateway, then test both direct and Gateway HTTP/SSE.
A healthy EPP Pod does not prove discovery: require
llm_d_epp_ready_endpoints{name="llmd"} and successful requests. With multiple
real workers, inspect per-worker queues/KV metrics and request counters under
uneven load. No live llm-d routing, cancellation, worker-failure, or GPU/P/D
validation is claimed by this repository migration.

Applying Kustomize does not prune the previous vLLM Router. After successful
cutover remove only its retained legacy resources:

```sh
kubectl delete -k deploy/kubernetes/base/router
```

For rollback, first restore the old router, then restore the Gateway upstream:

```sh
kubectl apply -k deploy/kubernetes/base/router
kubectl rollout status deployment/vllm-router -n inference
kubectl set env deployment/inference-gateway -n inference UPSTREAM_URL=http://vllm-router:30000
kubectl rollout status deployment/inference-gateway -n inference
```

The rollback is a runtime override; the next application apply restores llm-d.
Historical experiment instructions and recorded results may still reference
vLLM Router. Adapted cache/autoscaling runners now target llm-d and must be rerun
in new output directories before drawing comparisons. Autoscaling retains the
router_workers output key, now measuring EPP ready endpoints rather than the
old router's active-worker count.

For teardown, delete application resources before uninstalling Helm. Keep
cluster-scoped CRDs if anything else uses them:

```sh
helm uninstall llmd -n inference
```

EPP/Envoy Pods do not carry the legacy app label. The log dashboards derive
llmd-epp from the Pod name; Alloy namespace collection remains unchanged.

Sources: [llm-d standalone](https://llm-d.ai/docs/architecture/router/proxy),
[chart versions](https://github.com/llm-d/llm-d/blob/v1.0.0/guides/env.sh).

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
