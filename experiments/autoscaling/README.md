# Local vLLM autoscaling

Run commands from the repository root. See [deployment](../../docs/deployment.md)
for platform prerequisites.

The router balances traffic; KEDA reads the workers' running and waiting request
counts from Prometheus and creates an HPA for `mock-vllm`. The target is two
requests per replica, with one to three replicas. Scale-up adds at most one Pod
per minute; scale-down waits five minutes and removes at most one Pod per minute.
Missing metrics report a scaler error instead of zero load.

## Control loop and scope

```text
Router -> selected worker -> running/waiting metrics -> Prometheus
                                                        |
                                   KEDA -> HPA <----------+
                                            |
                                  Deployment replica count
```

Router selects endpoints; it does not change replica counts. The scaler query
sums running and waiting requests for `mock-qwen` on healthy `mock-vllm` targets.
KEDA exposes the external metric to HPA with `AverageValue` target 2. Kubernetes
then creates or deletes worker Pods. Model-file persistence avoids repeated
downloads, not all model startup work.

This procedure targets the standard CPU overlay, not MP-backed cache scaling.
It does not prove graceful migration or resumption of active generations.
The runner defines verification criteria; only saved successful live-run
artifacts establish that those criteria held for a particular deployment.

Install Prometheus, then deploy the CPU overlay and worker monitoring before
enabling autoscaling:

```sh
helm upgrade --install monitoring \
  oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack \
  --version 92.16.1 --namespace monitoring --create-namespace \
  --values deploy/observability/prometheus/values-local.yaml --wait --timeout 10m
kubectl apply -k deploy/kubernetes/overlays/local-vllm-cpu
kubectl rollout status deployment/mock-vllm -n inference --timeout=15m
kubectl apply -f deploy/observability/prometheus/monitors
helm upgrade --install keda keda \
  --repo https://kedacore.github.io/charts --version 2.20.0 \
  --namespace keda --create-namespace \
  --values deploy/platform/keda/keda-values-local.yaml \
  --wait --timeout 5m
kubectl apply -k deploy/kubernetes/components/autoscaling
kubectl get scaledobject,hpa,pods -n inference
kubectl get hpa -n inference -w
```

The CPU profile uses vLLM `0.31.0-arm64`, requests 4 GiB and limits memory to
6 GiB per replica. Check node capacity for three replicas plus monitoring and
system workloads; a nominal node allocation alone does not guarantee fit. The
shared ReadWriteOnce model-file cache is intended for a single-node setup.
Multi-node deployments need a separate model-file storage strategy.

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
