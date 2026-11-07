# Inference gateway

Go API gateway that proxies OpenAI-compatible chat completion requests to vLLM
Router. The router discovers ready workers through the Kubernetes API and
balances requests between them.

Run a local worker:

```sh
docker compose up -d
```

Run the gateway:

```sh
cd gateway
go run ./cmd
```

Run tests:

```sh
cd gateway
go test ./...
```

## Kubernetes

Build the local images and load them into the kind cluster. Kubernetes pulls
the pinned vLLM Router image from Docker Hub when the router is deployed.

```sh
docker build -t mock-vllm:v0.1 workers
docker build -t inference-gateway:v0.1 gateway
kind load docker-image mock-vllm:v0.1 inference-gateway:v0.1 --name inference-lab
```

Deploy the workers, vLLM Router, and gateway:

```sh
kubectl apply -k deployment/kubernetes
kubectl rollout status deployment/mock-vllm -n inference
kubectl rollout status deployment/vllm-router -n inference
kubectl rollout status deployment/inference-gateway -n inference
```

On an ARM64 development cluster, deploy a real CPU-backed vLLM worker instead
of the mock workers:

```sh
kubectl apply -k deployment/overlays/local-vllm-cpu
kubectl rollout status deployment/mock-vllm -n inference --timeout=15m
```

This profile runs one `HuggingFaceTB/SmolLM2-135M-Instruct` instance and preserves the
`mock-qwen` API model name. The first start downloads the model into a persistent
cache and can take several minutes.

Expose the gateway locally:

```sh
kubectl port-forward service/inference-gateway -n inference 8000:8000
```

Send a request through the gateway:

```sh
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"mock-qwen","messages":[{"role":"user","content":"Hello"}]}'
```

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

## Local vLLM autoscaling

The router balances traffic; KEDA reads the workers' running and waiting request
counts from Prometheus and creates an HPA for `mock-vllm`. The target is two
requests per replica, with one to three replicas. Scale-up adds at most one Pod
per minute; scale-down waits five minutes and removes at most one Pod per minute.
Missing metrics report a scaler error instead of zero load.

Install Prometheus, then deploy the CPU overlay and worker monitoring before
enabling autoscaling:

```sh
helm upgrade --install monitoring \
  oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack \
  --version 92.16.1 --namespace monitoring --create-namespace \
  --values deployment/observability/values-local.yaml --wait --timeout 10m
kubectl apply -k deployment/overlays/local-vllm-cpu
kubectl rollout status deployment/mock-vllm -n inference --timeout=15m
kubectl apply -f deployment/observability/monitors
helm upgrade --install keda keda \
  --repo https://kedacore.github.io/charts --version 2.20.0 \
  --namespace keda --create-namespace \
  --values deployment/kubernetes/keda/keda-values-local.yaml \
  --wait --timeout 5m
kubectl apply -k deployment/kubernetes/autoscaling
kubectl get scaledobject,hpa,pods -n inference
kubectl get hpa -n inference -w
```

The CPU profile uses vLLM `0.31.0-arm64`, requests 4 GiB and limits memory to
6 GiB per replica. Three replicas fit the current single-node kind allocation
of approximately 23 GiB. The shared ReadWriteOnce cache is intended for this
single-node setup. Multi-node deployments need a separate cache strategy.

Do not reapply the base deployment or CPU overlay while the ScaledObject is
active: their fixed replica counts conflict with HPA ownership. Roll back before
redeploying or rerunning E2E:

```sh
kubectl delete scaledobject vllm-worker -n inference
kubectl scale deployment/mock-vllm -n inference --replicas=1
```

This keeps the model cache. After rollback, run the complete local autoscaling
test (Redis and its `redis-auth` Secret must already exist):

```sh
E2E_AUTOSCALING=1 ./tests/e2e/pipeline.sh
```

This mode deploys the CPU overlay, monitoring and KEDA, then tests `1 -> 3 -> 1`.
Gateway smoke requests keep the normal rate limits. Six concurrent requests go
directly through the router, each generating 512 tokens, so the gateway's 60 RPM
limit does not constrain the scaling experiment. The test requires each worker
to complete requests, rejects HTTP errors and worker restarts, and allows ten
minutes for each scaling direction. Timestamped metrics and request logs are
preserved in the printed temporary artifact directory. Cleanup stops test traffic
and port-forwards; the deployed autoscaler remains active.
