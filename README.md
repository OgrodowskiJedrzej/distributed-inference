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
and mock workers. To additionally require traffic to reach at least two workers:

```sh
E2E_REQUESTS=10 E2E_MIN_WORKERS=2 ./tests/e2e/pipeline.sh
```
