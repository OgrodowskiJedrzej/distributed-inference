# Local Kubernetes quickstart

Run commands from the repository root. Requires Docker, kind, kubectl, and a
running `kind-inference-lab` cluster. This procedure assumes Redis is already
available in `inference` at `inference-redis-master:6379`, with its password
in the `redis-auth` Secret under `redis-password`. See
[deployment](deployment.md) for platform configuration.

Use the `kind-inference-lab` kubectl context. This applies fixed replicas to
existing Deployments; disable any worker autoscaler first using the
[rollback procedure](../experiments/autoscaling/README.md). Helm is required for llm-d and the platform/monitoring stacks; curl is used below.

Build and load the gateway and mock worker:

```sh
docker build -t mock-vllm:v0.1 tests/fixtures/mock-worker
docker build -t inference-gateway:v0.1 components/gateway
kind load docker-image mock-vllm:v0.1 inference-gateway:v0.1 --name inference-lab
```

Deploy llm-d standalone and the pipeline (no P/D):

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
kubectl port-forward service/inference-gateway -n inference 8000:8000
```

In a separate terminal:

```sh
curl -i http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"mock-qwen","messages":[{"role":"user","content":"Hello"}]}'
```

The response contains the mock worker's identity. To run a real model, use the
[ARM64 CPU profile](deployment.md#arm64-cpu-profile).

The response header contains a Gateway-generated `X-Request-ID`. This request
passes Redis admission, llm-d endpoint selection and mock worker execution; it does not
exercise real inference or external KV. Read [architecture](architecture.md)
for the flow and [observability](observability.md) to inspect logs in Grafana.
The port-forward remains running until stopped with Ctrl-C.
