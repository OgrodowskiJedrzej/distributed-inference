# External KV cache: one CPU worker

Run commands from the repository root. See [deployment](../../docs/deployment.md)
for prerequisites and the CPU profile.

The optional profile uses vLLM `0.31.0` and LMCache commit
`d0959e006063dcd8addd1b441d8ee5dd755ef366`. An independent `kv-cache` Pod owns
the KV tensors in RAM (1 GiB L1, LRU). Engine-driven inline transport copies
tensors over TCP; the Pods do not share `/dev/shm`. Restarting the cache loses
its contents. The existing PVC still holds model files, not runtime KV tensors.
This is a single-worker compatibility experiment, not prefill/decode splitting.

Disable autoscaling before applying the fixed-replica profile. Stop benchmark
clients and other inference traffic. Keep the model revision unchanged while
the cache is running; a model/layout change requires recreating the cache.

```sh
kubectl delete scaledobject vllm-worker -n inference --ignore-not-found
kubectl get hpa -n inference
docker build -t inference-vllm-lmcache:0.31.0-d0959e0 integrations/lmcache
kind load docker-image inference-vllm-lmcache:0.31.0-d0959e0 --name inference-lab
kubectl apply -k deploy/kubernetes/overlays/local-vllm-kv-cache
kubectl rollout status deployment/kv-cache -n inference --timeout=5m
kubectl rollout status deployment/mock-vllm -n inference --timeout=15m
python3 tests/e2e/external_kv_cache.py --output /tmp/external-kv-cache-run-001
```

The output directory must not already exist. Two one-token generations with an
unrelated prefix initialise LMCache's lazy metric counters. The test then sends
a unique 512-token prompt through the router, checks KV writes, replaces only the worker, then
requires external hits, KV reads, at least 128 fewer computed prefill tokens,
and identical greedy output. Local prefix caching is disabled to isolate the
external cache. Raw metrics, requests, responses and Pod identities are saved;
failures also save logs. Port-forwards are stopped, deployments remain running.
No TTFT improvement is assumed or required on CPU.

The base image is pinned by digest and LMCache by commit. Transitive Python
dependencies are resolved during the build; the resolved versions are recorded
inside the image at `/opt/lmcache-installed.txt`. Reuse the same built image for
comparisons rather than assuming separate builds are identical. LMCache's older
OpenTelemetry dependencies replace those in the base image; the now-unused
`opentelemetry-exporter-otlp-common` package is removed and `pip check` must pass.

Restore the standard worker before removing the experimental cache:

```sh
kubectl apply -k deploy/kubernetes/overlays/local-vllm-cpu
kubectl rollout status deployment/mock-vllm -n inference --timeout=15m
kubectl delete deployment/kv-cache service/kv-cache -n inference
# Optional: restore autoscaling after the standard worker is ready.
kubectl apply -k deploy/kubernetes/components/autoscaling
```

This does not remove the model-cache PVC. Cache transport failure behavior and
multi-worker cache sharing are not validated by this first test.
