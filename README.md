# Inference gateway

Go API gateway that proxies OpenAI-compatible chat completion requests to one
logical vLLM upstream. Worker discovery and load balancing belong to the vLLM
or Kubernetes routing layer, not to this process.

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
