# Pipeline benchmark

This runner sends streaming requests from a Kubernetes Pod through
`inference-gateway:8000`. It neither changes gateway limits nor prepares caches,
restarts workers, or changes KEDA. Existing router-only experiments are separate.

The first implementation unit provides the runner and client. The dashboard,
PodMonitor, LMCache ServiceMonitor and load-profile overlay are separate units;
until installed, the printed Grafana link and Prometheus scraping are not active.

## Run

Requires `kind-inference-lab`, kubectl, Python 3 on the host, a ready pinned CPU
worker, gateway, router and Redis. The worker image must already include aiohttp,
transformers and vLLM. No packages are installed by this runner. The client uses
the worker's pullable image digest and read-only model-cache PVC on the same node.
The configured tokenizer revision must already exist in that cache.

```sh
python3 benchmarks/pipeline/run.py \
  --config benchmarks/pipeline/workloads.json \
  --output benchmark-results/pipeline-001
```

The output directory must not exist. Only one benchmark may run against this
namespace; remove other traffic yourself. Existing benchmark Pods block startup,
including Pods retained for recovery. The runner reads live deployment, HPA,
ScaledObject and Pod state without requiring stable worker identities.

The example uses the normal gateway profile (60 RPM, 100000 TPM, concurrency 8).
It is expected to produce rejections in higher-rate stages. The load profile
requires explicit prior deployment of limits 6000 RPM, 10000000 TPM, concurrency
256 and `gateway_profile: "load"` in a separate JSON config. The runner verifies
the declared profile against the running gateway's config and does not switch it.
Copy the example config to a new explicit path to change stages, seed, timeout or
workload. The model revision, served name and context length must match the worker.

Stages specify durations and rates, including zero-rate idle stages. Poisson
arrival timing and prompt sampling use separate RNGs with the recorded seed.
There is no warm-up, automatic retry, redirect following or hidden HTTP connector
concurrency limit. The explicit client limit is 256 requests. Arrivals above that
limit are counted as unsent, as are arrivals delayed by more than 100 ms by the
generator. Late arrivals are not sent in a catch-up burst. Scheduler lag is
measured after prompt preparation; generator CPU and RSS are also exported.
Draining lasts at most one configured request timeout after the final stage.
Request errors do not invalidate an otherwise completed load experiment.

## Measurements

Client version: `pipeline-stream-v1`. Prompt generator: `stable-word-tokens-v1`,
reused from the saved word-based benchmark: 64 shared single-token words plus a
seeded suffix. Requested input length excludes the chat template; actual lengths
are checked and saved. Generation is greedy with fixed requested output length
and EOS ignored. A 64-token shared prefix need not fill a worker KV cache block;
cache ON is not assumed to improve these workloads.

TTFT ends at first nonempty content; E2E ends at last nonempty content. Chunk
intervals measure received content fragments, not individual tokens. Protocol
tail measures last content to DONE. Usage-only, role-only, finish-only events and
comments do not create latency samples. Missing DONE, broken streams, malformed
JSON, timeouts and cancellations do not contribute successful latency samples.
Latency histograms are committed only after successful stream completion.
These definitions/version must not be treated as identical to historical native
`vllm bench` results. Histogram percentiles are approximations, not exact samples.

Metrics use the `pipeline_client_` prefix and a `run_id` label. `info` carries
client version, seed, declared gateway profile and workload. Numeric `phase` is
0 initializing, 1 scheduling, 2 draining, 3 complete, 4 incomplete; `stage` is
zero-based. Counters distinguish scheduled, sent, completed by outcome/status/
reason, unsent by reason, output tokens, usage-covered and missing-usage requests.
Histogram buckets in seconds: .001, .005, .01, .025, .05, .1, .25, .5, 1, 2.5,
5, 10, 30, 60, 120, 300, +Inf. Metrics remain available for 15 seconds after run
completion before status publication and collection.

Output tokens are taken only from successful-stream usage. Do not interpret a
zero output-token rate as measured zero when usage coverage is absent. Plot
`usage_requests_total` and `missing_usage_total` alongside it. CPU is cumulative
process seconds; RSS is current resident memory on Linux.

`completed.jsonl` and stdout contain request outcomes, timings and gateway
`X-Request-ID` where available; they contain neither prompt bodies nor response
text. `workload.jsonl` intentionally saves the synthetic prompts and their indices,
including unsent arrivals, for reproducibility. Request indices connect the two.

## Artifacts and failure handling

The new output directory contains source/config copies, their manifest/hash,
Git revision/dirty flag, generated Pod manifest, initial/final state, a periodic
state timeline, client logs, client results, exact timing samples and final
Prometheus exposition. Runtime library versions and workload hash are saved by
the client. Pod state includes resolved image IDs; manifests retain configuration
and secret references, not Secret contents. Gateway policy collection records
only rate limits and reservation TTL, not Redis credentials.

`status.json` records completion, collection/cleanup failures and a Grafana URL
for this run's client time interval. Runner exit status is nonzero for incomplete
runs or orchestration/collection failures, not for individual rejected requests.
Ctrl-C asks the client to stop and collect partial results. If collection fails,
the Pod and ConfigMap are retained to preserve its emptyDir; recover artifacts
before deleting them. Cleanup only deletes resources created for this run.
The runner never cleans or rolls back server deployments or experiment outputs.

Component metrics belong to the selected time interval and deployment; they do
not carry this run ID. CPU hardware remains shared, and co-location can affect
results. Autoscaling still depends on cluster resources, gateway admission and
KEDA policy. The normal load example does not prove scaling. MP-backed cache
scaling is not established by the existing fixed-replica cache tests.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s benchmarks/pipeline -p 'test_*.py'
```

HTTP/SSE tests require aiohttp and otherwise report skips. Run them in the
already-built worker image to exercise real HTTP without installing host packages:

```sh
docker run --rm --network none \
  --mount type=bind,src="$PWD/benchmarks/pipeline",dst=/tests,readonly \
  --entrypoint python3 inference-vllm-lmcache:0.31.0-d0959e0 \
  -B -m unittest discover -s /tests -p 'test_*.py'
```

These tests use only a local controlled SSE server and mocked Kubernetes calls;
they do not create cluster resources or send inference traffic.
