# Local Kubernetes quickstart

Run commands from the repository root. Requires Docker, kind, kubectl, and a
running `kind-inference-lab` cluster. This procedure assumes Redis is already
available in `inference` at `inference-redis-master:6379`, with its password
in the `redis-auth` Secret under `redis-password`. See
[deployment](deployment.md) for platform configuration.

Use the `kind-inference-lab` kubectl context. This applies fixed replicas to
existing Deployments; disable any worker autoscaler first using the
[rollback procedure](../experiments/autoscaling/README.md). Helm is needed if
installing the platform or monitoring stacks; curl is used below.

Build and load the gateway and mock worker:

```sh
docker build -t mock-vllm:v0.1 tests/fixtures/mock-worker
docker build -t inference-gateway:v0.1 components/gateway
kind load docker-image mock-vllm:v0.1 inference-gateway:v0.1 --name inference-lab
```

Deploy the pipeline:

```sh
kubectl apply -k deploy/kubernetes/base
kubectl rollout status deployment/mock-vllm -n inference
kubectl rollout status deployment/vllm-router -n inference
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
passes Redis admission, Router selection and mock worker execution; it does not
exercise real inference or external KV. Read [architecture](architecture.md)
for the flow and [observability](observability.md) to inspect logs in Grafana.
The port-forward remains running until stopped with Ctrl-C.
