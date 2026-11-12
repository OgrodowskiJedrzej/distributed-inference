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

The local logging stack keeps seven days of Pod logs in a 10 GiB Loki PVC.
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
