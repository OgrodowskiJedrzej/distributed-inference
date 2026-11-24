# Observability

Run commands from the repository root. Metrics and log storage are installed
in the `monitoring` namespace.

## Metrics

```sh
helm upgrade --install monitoring \
  oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack \
  --version 92.16.1 --namespace monitoring --create-namespace \
  --values deploy/observability/prometheus/values-local.yaml --wait --timeout 10m
kubectl apply -f deploy/observability/prometheus/monitors
```

The monitoring values also provision the Loki datasource in Grafana.

## Request logs

The local logging stack configures seven-day retention on a 10 GiB Loki PVC;
available space still depends on log volume. Loki is single-instance with local
filesystem storage, not an HA or backed-up deployment.
Grafana Alloy collects container stdout; applications do not push logs directly
to Loki. Gateway and mock worker records are JSON and share `X-Request-ID`.

```sh
helm upgrade --install loki \
  oci://ghcr.io/grafana-community/helm-charts/loki \
  --version 18.13.8 --namespace monitoring --create-namespace \
  --values deploy/observability/loki/values-local.yaml \
  --wait --timeout 10m
helm upgrade --install alloy alloy \
  --repo https://grafana.github.io/helm-charts --version 1.15.0 \
  --namespace monitoring \
  --values deploy/observability/alloy/values-local.yaml \
  --wait --timeout 10m
helm upgrade --install monitoring \
  oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack \
  --version 92.16.1 --namespace monitoring --create-namespace \
  --values deploy/observability/prometheus/values-local.yaml --wait --timeout 10m
kubectl apply -f deploy/observability/grafana/dashboards
kubectl port-forward -n monitoring service/monitoring-grafana 3000:80
```

Open `http://127.0.0.1:3000`, select **Inference request logs**, and enter a
request ID returned in the `X-Request-ID` response header. The default local
Grafana credentials are stored in the `monitoring-grafana` Secret.

Alloy discovers Pods only in `inference`. Its read positions are stored on
each node at `/var/lib/alloy/inference-logs`; they survive Alloy Pod replacement
on the same node. Local node or kind cluster deletion can remove both positions
and locally provisioned storage.

The Gateway generates `X-Request-ID`, forwards it upstream, and returns it in
the response. The mock worker logs request start, completion, and failure.
Router and real vLLM logs retain their upstream formats.

## What can be correlated

Gateway emits `request_completed` with request ID, Pod, method, path, status
and duration. Its custom logger skips `/healthz` and `/metrics`; startup and
other library messages may still be plain text. Mock chat logs include start,
completion and failure. Router, real vLLM and LMCache are external implementations:
forwarding the header does not guarantee they print it in every stage.

The dashboard's request-ID filter is a raw-line regex search, not an exact JSON
field join. For an exact Gateway match, use Grafana Explore with Loki:

```logql
{namespace="inference", app="inference-gateway"} | json | request_id="YOUR_ID"
```

To find all records that actually include the ID:

```logql
{namespace="inference"} |= "YOUR_ID"
```

This is log correlation, not distributed tracing: there are no spans or reliable
per-stage timings. Custom Gateway/mock records do not log prompt bodies, but
that is not a privacy guarantee for upstream images. Alloy collects only the
`inference` namespace; rotated logs during outages may be lost. Node deletion
can also remove locally provisioned PVC data. No exactly-once delivery is claimed.

## Metric coverage

ServiceMonitors cover Gateway, Router and workers. Gateway exports
`gateway_http_requests_total`, `gateway_http_request_duration_seconds` and
`gateway_http_requests_in_flight`, plus Go/process metrics. Worker running and
waiting request counts feed the autoscaling experiment.

The LMCache ServiceMonitor scrapes `kv-cache:8080/metrics`. Its endpoint relabel
keeps only the `kv-cache` Service by name because the current Service has no app
label. It does not scrape other Services despite the empty discovery selector.
Without the optional cache deployment there are no LMCache targets or samples.
The cache tests still retain their own raw metric snapshots.

The benchmark PodMonitor selects `app=pipeline-benchmark` and scrapes its named
`metrics` port every five seconds. It carries `release: monitoring` to match the
Helm release used above, including the chart's default PodMonitor selector.
If the release or selector is customized, adapt that label before installation.
Client metric labels carry the run ID; existing component metrics do not.
Alloy's existing namespace discovery also collects the client's JSON stdout as
`app=pipeline-benchmark`, without additional application log configuration.

Prometheus retains six hours without a configured PVC. Default alerting rules
and Alertmanager are disabled. The request-log and pipeline experiment
dashboards are provisioned separately from `deploy/observability/grafana/dashboards`.

## Pipeline benchmark and gateway profiles

See [pipeline benchmark](../benchmarks/pipeline/README.md) for the generator,
configuration, metric definitions, artifacts and test commands. Install both new
monitors with the existing directory command before running experiments:

```sh
kubectl apply -f deploy/observability/prometheus/monitors
kubectl get servicemonitor,podmonitor -n inference
```

Check the Prometheus Targets page: a running benchmark should have an `up` series
for its Pod, and a deployed cache should have one for `service="kv-cache"`.
A monitor object alone does not prove successful discovery or scraping. Client
metrics are exposed during the run and for fifteen seconds after completion.
Absent targets/series must not be interpreted as measured zero load or cache use.

The normal gateway profile uses its existing image configuration: 60 RPM,
100000 TPM, and concurrency 8. The optional load overlay sets 6000 RPM,
10000000 TPM, and concurrency 256. It mounts a generated, versioned ConfigMap at
`/app/config.yaml`; Redis/upstream environment overrides remain unchanged.
Admission remains enabled. Resource limits and routing policy do not change.

Stop benchmark traffic before switching profiles. Applying this overlay rolls
the gateway; it does not apply worker deployments or modify HPA/KEDA ownership:

```sh
kubectl kustomize deploy/kubernetes/overlays/local-gateway-load
kubectl apply -k deploy/kubernetes/overlays/local-gateway-load
kubectl rollout status deployment/inference-gateway -n inference
```

Use a separate experiment config with `gateway_profile: "load"`. The runner
checks the running gateway's limits and never deploys either profile. Higher
limits do not guarantee that the generator, gateway or cluster can sustain the
requested RPS. The example workload defaults to the normal profile and may
therefore produce admission rejections before the workers saturate.

Restore the image's normal configuration after stopping traffic:

```sh
kubectl apply -k deploy/kubernetes/base/gateway
kubectl rollout status deployment/inference-gateway -n inference
```

Apply and restore using these same client-side `kubectl apply` commands so that
removal of the overlay's volume/mount is tracked. The generated load ConfigMap
may remain unused; it does not affect the normal gateway without the mount.
Do not reapply a worker overlay to switch gateway profiles while KEDA is active.
Existing Redis rate-limit state is not reset by either profile switch: its
current window and outstanding reservations can affect the start of a run.

## Pipeline experiment dashboard

Install the monitors as above, then provision the dashboard:

```sh
kubectl apply -f deploy/observability/grafana/dashboards
kubectl port-forward -n monitoring service/monitoring-grafana 3000:80
```

Open **Inference pipeline experiment** (`inference-pipeline-experiment`). Select
its Prometheus datasource, Run ID and time interval, or use the link printed by
`benchmarks/pipeline/run.py`. Run IDs and Pod options are discovered in the
selected time range. A run must have been scraped to appear. No client data
before installing the PodMonitor can be reconstructed by this dashboard.
The existing Loki datasource must be installed for the log panels.

The sections cover run metadata and scrape health, offered/sent/completed load,
errors and admission reasons, successful client latency, generator capacity,
gateway traffic, worker execution/routing membership, local/external cache,
autoscaling/Pod lifecycle, and logs. Rows can be collapsed in Grafana. Metadata
and phase show the last scraped value in the selected interval, including for
completed runs whose client Pod has been removed.

**Correlation and filters.** Only client metrics and client JSON records are
filtered by Run ID. Component metrics include all traffic to the deployment in
the selected wall-clock interval; keep other traffic stopped. Worker details
filter only worker execution and cache panels. Scaling always covers all worker
replicas, including new Pods, and reproduces the current ScaledObject load query.
HPA panels use the default `keda-hpa-vllm-worker` name. A custom HPA name or changed
scaler policy requires corresponding dashboard queries to be updated.

The component-log panel uses its own component/Pod filters and a literal
request-ID substring search; an empty request ID includes all lines. The client
panel additionally filters the parsed JSON `run_id` exactly and retains only
parseable client records. It ignores component/Pod filters so the chosen run
remains visible. Both log panels use the request-ID search. This does not create
a distributed trace; upstream components can omit request IDs.

**Measurement boundaries.** Client latency includes the gateway and network
inside the cluster. Gateway HTTP duration includes rejected and failed chat
requests; client latency histograms include only completed successful streams.
Client chunk intervals are not individual token intervals. Server prefill,
decode, queue and LMCache lookup timings come directly from their histograms;
subtracting unrelated percentiles does not estimate another stage's latency.
Completed work per Pod is not an exact measurement of router dispatch decisions.

`rate` and `increase` are applied to individual counters before aggregation so
worker replacement does not combine resets. Quantiles are approximations from
histogram buckets over `$__rate_interval`; the window is at least suitable for
the five-second scrape cadence. The latency-observation panel shows extrapolated
sample increments, which can be fractional and can miss the first increment of
a newly appearing series. Short runs and sparse tails need raw artifact review.

Cache ratios are token weighted, with a positive query denominator required.
LMCache's chunk-aligned denominator differs from whole-prompt and vLLM cache
counters. Absent targets, absent series, zero-denominator ratios and empty
histograms remain missing/undefined rather than being filled with zero. Output
tokens/s is displayed only when that rate window has usage-covered successes
and no missing-usage successes; requests/s coverage remains visible separately.
Scrape `up=1` means the endpoint answered, not that its pipeline is making progress.

The runner link ends at client completion. Final metrics can first be scraped
during the following fifteen-second hold, so extend the time picker by twenty
seconds after completion when checking final phase and cumulative counts.
These counts are the last scraped counters, not exact artifact totals. Use
`completed.jsonl`, `metrics.prom` and client `status.json` for final records.

CPU and memory panels use exported process metrics; they do not imply collection
of container throttling, GPU utilization, Redis internals, or KEDA operator logs.
The existing Alloy configuration collects only namespace `inference`. Optional
LMCache/HPA panels can remain empty when those components are disabled. Runtime
scraping, Loki queries, scale-up and scale-down require separate live verification;
provisioning a dashboard does not establish that these experiments passed.
