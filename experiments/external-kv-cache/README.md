# External KV cache

Run commands from the repository root. See [deployment](../../docs/deployment.md)
for prerequisites and the CPU profile.

The optional profile uses vLLM `0.31.0` and LMCache commit
`d0959e006063dcd8addd1b441d8ee5dd755ef366`. An independent `kv-cache` Pod owns
the KV tensors in RAM (1 GiB L1, LRU). Engine-driven inline transport copies
tensors over TCP; the Pods do not share `/dev/shm`. Restarting the cache loses
its contents. The existing PVC still holds model files, not runtime KV tensors.
Two profiles cover worker replacement and sharing between live replicas. Each
worker performs both prefill and decode; neither profile uses P/D splitting.

## Evidence and interpretation

| Procedure | Intended evidence | Does not establish |
| --- | --- | --- |
| Single worker | KV survives worker replacement while MP remains alive | Sharing between live replicas |
| Two workers | A-to-B and B-to-A prefix reuse, continuation and replacement | Cache-aware routing, throughput gains or HA |

The patched image passed all nine live two-worker scenarios on 2026-11-16.
Local evidence is retained under
`artifacts/runs/shared-kv-heartbeat-20261116-01/summary.json` and the accompanying
raw metrics, responses and Pod identities. Both reuse directions and replacement
reported 511 external hit tokens and one computed prefill token for a 512-token
prompt. MP was not restarted. This establishes those scenarios for this local
deployment, not general availability or throughput improvement.

The client still supplies all context. Cache lookup uses token prefixes and
compatible model identity, not a conversation ID or request ID. Router remains
`power_of_two`; both workers use the same MP endpoint. Redis is used only by
Gateway admission, not as a KV backend. GPU, cross-node and mixed TP/PP layouts
are outside this experiment. A missing prefix can be recomputed; connector
transport failure and MP reconnection are separate, unvalidated cases.

## Single-worker replacement

### CPU worker heartbeat fix

The image applies
`integrations/lmcache/patches/start-heartbeat-after-registration.patch` to the
pinned LMCache source. After successful CPU KV registration, heartbeat starts
immediately instead of waiting for the first cache operation. CUDA registration
keeps its upstream behavior. The existing heartbeat start is idempotent and its
existing shutdown path remains unchanged.

Without this patch, an idle worker can outlive the MP registration grace period:
MP logs `Reaped non-GPU instance ... (pinged=False)`, then rejects stores with
`non-GPU context not registered (or reaped)`. PING still reports the server as
healthy, so this does not trigger the recovery callback. Increasing the test
bootstrap timeout does not fix it.

To verify the fix, rebuild and load the image, then restart workers without
restarting MP. Confirm each worker starts `lmcache-heartbeat` immediately after
KV registration, before client traffic. Leave them idle beyond MP's configured
registration grace and reap timeout (the observed unpatched grace was one hour),
then run the sharing test in a new output directory. Require no reaping of live
workers and successful external reads/writes. This is a verification procedure,
not a claim that the idle-duration or sharing test has passed. Recovery after
MP restart is outside this fix.

Build, `pip check`, registration-branch checks and live startup ordering passed.
A simulated-clock regression using the actual MP touch/reaper methods retained
the registration with ten-second heartbeats across 3720 seconds; without them
it was reaped. A real hour-long idle run has not been performed. Upstream MP
liveness tests could not run in the CPU-only image because their test package
requires `lmcache.lmcache_native`.

The local image tag is unchanged, so already-running Pods do not acquire the
patch automatically: load the rebuilt image into kind and restart workers.
The verified build's image manifest digest is
`sha256:3030bc7ba199dac1ffc2b392501e7194aaf47bb48ce562f6b5236e1bd7552624`.
Worker image IDs are also saved in the run artifacts. Dependency resolution
still occurs during builds; this patch does not add dependency locking.

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

This does not remove the model-cache PVC. The single-worker test does not
validate sharing between live replicas or cache transport failure behavior.

## Two-worker sharing

The multi-worker overlay inherits the single-worker configuration and changes
only the worker replica count to two. Both workers connect to the same MP server
over TCP. The server retains 1 GiB of RAM-backed LRU cache. Local prefix caching
remains disabled so that reuse must come from the external cache. Model revision,
dtype, tokenizer, chunk size, connector, and image must match across replicas.

Use the same built image as above. Disable autoscaling and stop other inference
clients before applying the profile. This remains a single-node ARM64 experiment:
the model-file PVC is ReadWriteOnce, and the workers and cache must fit on the
same node. Two workers retain their existing limits of 6 GiB each; the cache
retains its 4 GiB limit. Monitoring and other workloads need additional headroom.

```sh
kubectl delete scaledobject vllm-worker -n inference --ignore-not-found
kubectl get hpa -n inference
kubectl apply -k deploy/kubernetes/overlays/local-vllm-kv-cache-multi
kubectl rollout status deployment/kv-cache -n inference --timeout=5m
kubectl rollout status deployment/mock-vllm -n inference --timeout=15m
python3 tests/e2e/shared_kv_cache.py --output artifacts/runs/shared-kv-001
```

The test requires the `kind-inference-lab` context and a new output directory.
It sends requests directly to specific Pod endpoints to prove A-to-B and B-to-A
reuse, then checks concurrent reads of shared and distinct prefixes. Different
prefixes are identified by unique tokens, not conversation IDs. A token-level
continuation extends the original 512-token context by 64 tokens and compares
outputs across workers. It exercises prefix extension rather than chat-template
or application-side conversation storage.

The test also checks a request through the router, deletes exactly one worker
Pod, verifies that its peer still serves requests, waits for a replacement, and
requires the replacement to retrieve KV written by the other worker. The shared
cache Pod must keep its identity and restart count throughout. Worker replacement
is intentional; all other worker restarts invalidate the experiment.

Each measured warm request must report at least 128 external hit tokens and at
least 128 fewer computed prefill tokens than its full prompt length, together
with external reads and matching greedy outputs. Cold requests must compute
their complete prompt. Initialization uses unrelated prefixes to populate lazy
metric series; measurement then polls for the expected counters with a timeout.
Raw metrics from every poll, requests, responses, Pod identities, and a summary
are retained. Existing outputs are never overwritten. Deployment state remains
running when the test finishes; port-forwards are stopped.

This profile validates two replicas only after a successful live test. It does
not establish throughput gains, single-flight prefill deduplication, multi-node
storage, or recovery after an MP server restart. Restarting MP loses the cached
KV; connector reconnection and transport failure behavior still require a
separate experiment. No automatic failover or recompute-on-transport-error is
assumed. An evicted prefix can be recomputed from the request's complete input.

To return to the single-worker cache experiment:

```sh
kubectl apply -k deploy/kubernetes/overlays/local-vllm-kv-cache
kubectl rollout status deployment/mock-vllm -n inference --timeout=15m
```

Use the earlier standard-worker rollback before removing the external cache.
