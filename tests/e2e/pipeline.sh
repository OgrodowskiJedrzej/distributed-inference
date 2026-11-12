#!/usr/bin/env bash

set -euo pipefail

NAMESPACE="${E2E_NAMESPACE:-inference}"
KIND_CLUSTER="${E2E_KIND_CLUSTER:-inference-lab}"
GATEWAY_IMAGE="${E2E_GATEWAY_IMAGE:-inference-gateway:e2e}"
WORKER_IMAGE="${E2E_WORKER_IMAGE:-mock-vllm:e2e}"
LOCAL_PORT="${E2E_PORT:-18000}"
PROMETHEUS_LOCAL_PORT="${E2E_PROMETHEUS_PORT:-19090}"
LOKI_LOCAL_PORT="${E2E_LOKI_PORT:-13100}"
REQUESTS="${E2E_REQUESTS:-3}"
MIN_WORKERS="${E2E_MIN_WORKERS:-0}"
REQUEST_TIMEOUT_SECONDS="${E2E_REQUEST_TIMEOUT_SECONDS:-120}"
AUTOSCALING="${E2E_AUTOSCALING:-0}"
ROUTER_LOCAL_PORT="${E2E_ROUTER_PORT:-13000}"
MONITORING_NAMESPACE="${E2E_MONITORING_NAMESPACE:-monitoring}"
MONITORING_RELEASE="${E2E_MONITORING_RELEASE:-monitoring}"
MONITORING_CHART_VERSION="${E2E_MONITORING_CHART_VERSION:-92.16.1}"
MONITORING_HELM_TIMEOUT="${E2E_MONITORING_HELM_TIMEOUT:-10m}"
LOKI_CHART_VERSION="${E2E_LOKI_CHART_VERSION:-18.13.8}"
ALLOY_CHART_VERSION="${E2E_ALLOY_CHART_VERSION:-1.15.0}"
BASE_URL="http://127.0.0.1:${LOCAL_PORT}"
PROMETHEUS_BASE_URL="http://127.0.0.1:${PROMETHEUS_LOCAL_PORT}"
LOKI_BASE_URL="http://127.0.0.1:${LOKI_LOCAL_PORT}"
SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIRECTORY}/../.." && pwd)"

for command in docker kind kubectl helm curl python3; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "missing required command: ${command}" >&2
    exit 1
  fi
done

current_context="$(kubectl config current-context)"
if [[ "${current_context}" != "kind-${KIND_CLUSTER}" ]]; then
  echo "current Kubernetes context is ${current_context}, expected kind-${KIND_CLUSTER}" >&2
  exit 1
fi

installed_crds="$(kubectl get crd -o name)"
if [[ "${installed_crds}" == *"customresourcedefinition.apiextensions.k8s.io/scaledobjects.keda.sh"* ]]; then
  scaled_objects="$(kubectl get scaledobjects -n "${NAMESPACE}" -o name)"
  if [[ -n "${scaled_objects}" ]]; then
    echo "Remove the existing ScaledObject before redeploying workers; see README rollback commands." >&2
    exit 1
  fi
fi
if [[ "${AUTOSCALING}" == "1" ]]; then
  if [[ "${NAMESPACE}" != "inference" || "${MONITORING_NAMESPACE}" != "monitoring" || "${MONITORING_RELEASE}" != "monitoring" ]]; then
    echo "Autoscaling manifests require inference/monitoring namespaces and monitoring release." >&2
    exit 1
  fi
  MIN_WORKERS=0
fi

temporary_directory="$(mktemp -d)"
gateway_port_forward_pid=""
prometheus_port_forward_pid=""
router_port_forward_pid=""
loki_port_forward_pid=""
autoscaling_test_pid=""

cleanup() {
  local status=$?
  if [[ -n "${autoscaling_test_pid}" ]]; then
    kill -TERM "${autoscaling_test_pid}" >/dev/null 2>&1 || true
    wait "${autoscaling_test_pid}" >/dev/null 2>&1 || true
  fi
  if [[ "${AUTOSCALING}" == "1" && "${status}" != "0" ]]; then
    kubectl get scaledobject,hpa,deployment,pods -n "${NAMESPACE}" -o wide || true
    kubectl describe scaledobject vllm-worker -n "${NAMESPACE}" || true
    kubectl describe hpa -n "${NAMESPACE}" || true
    kubectl get events -n "${NAMESPACE}" --sort-by=.lastTimestamp || true
    kubectl logs -n "${NAMESPACE}" -l app=mock-vllm --all-containers --prefix --tail=100 || true
  fi
  for pid in "${gateway_port_forward_pid}" "${prometheus_port_forward_pid}" "${router_port_forward_pid}" "${loki_port_forward_pid}"; do
    if [[ -n "${pid}" ]]; then
      kill "${pid}" >/dev/null 2>&1 || true
      wait "${pid}" >/dev/null 2>&1 || true
    fi
  done
  if [[ "${AUTOSCALING}" == "1" ]]; then
    echo "Autoscaling artifacts: ${temporary_directory}"
  else
    rm -f "${temporary_directory}/gateway-port-forward.log"
    rm -f "${temporary_directory}/prometheus-port-forward.log"
    rm -f "${temporary_directory}/loki-port-forward.log"
    rm -f "${temporary_directory}/request-id"
    rmdir "${temporary_directory}"
  fi
}
trap cleanup EXIT

echo "Building and deploying gateway..."
docker build \
  --tag "${GATEWAY_IMAGE}" \
  "${REPOSITORY_ROOT}/components/gateway"
kind load docker-image \
  "${GATEWAY_IMAGE}" \
  --name "${KIND_CLUSTER}"
if [[ "${AUTOSCALING}" != "1" ]]; then
  docker build \
    --tag "${WORKER_IMAGE}" \
    "${REPOSITORY_ROOT}/tests/fixtures/mock-worker"
  kind load docker-image \
    "${WORKER_IMAGE}" \
    --name "${KIND_CLUSTER}"
fi

if [[ "${AUTOSCALING}" == "1" ]]; then
  kubectl apply -k "${REPOSITORY_ROOT}/deploy/kubernetes/overlays/local-vllm-cpu"
else
  kubectl apply -k "${REPOSITORY_ROOT}/deploy/kubernetes/base"
fi
kubectl set image \
  deployment/inference-gateway \
  gateway="${GATEWAY_IMAGE}" \
  -n "${NAMESPACE}"
if [[ "${AUTOSCALING}" != "1" ]]; then
  kubectl set image \
    deployment/mock-vllm \
    worker="${WORKER_IMAGE}" \
    -n "${NAMESPACE}"
fi

echo "Checking Kubernetes workloads..."
worker_rollout_timeout=60s
if [[ "${AUTOSCALING}" == "1" ]]; then
  worker_rollout_timeout=15m
fi
kubectl rollout status deployment/mock-vllm -n "${NAMESPACE}" --timeout="${worker_rollout_timeout}"
kubectl rollout status deployment/vllm-router -n "${NAMESPACE}" --timeout=60s
kubectl rollout status deployment/inference-gateway -n "${NAMESPACE}" --timeout=60s

echo "Deploying observability stack..."
helm upgrade --install "${MONITORING_RELEASE}" \
  oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack \
  --version "${MONITORING_CHART_VERSION}" \
  --namespace "${MONITORING_NAMESPACE}" \
  --create-namespace \
  --values "${REPOSITORY_ROOT}/deploy/observability/prometheus/values-local.yaml" \
  --wait \
  --timeout "${MONITORING_HELM_TIMEOUT}"

helm upgrade --install loki \
  oci://ghcr.io/grafana-community/helm-charts/loki \
  --version "${LOKI_CHART_VERSION}" \
  --namespace "${MONITORING_NAMESPACE}" \
  --values "${REPOSITORY_ROOT}/deploy/observability/loki/values-local.yaml" \
  --wait \
  --timeout "${MONITORING_HELM_TIMEOUT}"

helm upgrade --install alloy alloy \
  --repo https://grafana.github.io/helm-charts \
  --version "${ALLOY_CHART_VERSION}" \
  --namespace "${MONITORING_NAMESPACE}" \
  --values "${REPOSITORY_ROOT}/deploy/observability/alloy/values-local.yaml" \
  --wait \
  --timeout "${MONITORING_HELM_TIMEOUT}"

kubectl apply -f "${REPOSITORY_ROOT}/deploy/observability/prometheus/monitors"
kubectl apply -f "${REPOSITORY_ROOT}/deploy/observability/grafana/dashboards"

kubectl port-forward \
  -n "${NAMESPACE}" \
  service/inference-gateway \
  "${LOCAL_PORT}:8000" \
  >"${temporary_directory}/gateway-port-forward.log" 2>&1 &
gateway_port_forward_pid=$!

prometheus_service="${MONITORING_RELEASE}-kube-prometheus-prometheus"
kubectl port-forward \
  -n "${MONITORING_NAMESPACE}" \
  "service/${prometheus_service}" \
  "${PROMETHEUS_LOCAL_PORT}:9090" \
  >"${temporary_directory}/prometheus-port-forward.log" 2>&1 &
prometheus_port_forward_pid=$!

kubectl port-forward \
  -n "${MONITORING_NAMESPACE}" \
  service/loki-gateway \
  "${LOKI_LOCAL_PORT}:80" \
  >"${temporary_directory}/loki-port-forward.log" 2>&1 &
loki_port_forward_pid=$!

for _ in {1..30}; do
  if ! kill -0 "${gateway_port_forward_pid}" >/dev/null 2>&1; then
    cat "${temporary_directory}/gateway-port-forward.log" >&2
    exit 1
  fi
  if curl --fail --silent --show-error "${BASE_URL}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl --fail --silent --show-error "${BASE_URL}/healthz" >/dev/null; then
  echo "gateway did not become healthy" >&2
  cat "${temporary_directory}/gateway-port-forward.log" >&2
  exit 1
fi

for _ in {1..30}; do
  if ! kill -0 "${prometheus_port_forward_pid}" >/dev/null 2>&1; then
    cat "${temporary_directory}/prometheus-port-forward.log" >&2
    exit 1
  fi
  if curl --fail --silent --show-error "${PROMETHEUS_BASE_URL}/-/ready" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl --fail --silent --show-error "${PROMETHEUS_BASE_URL}/-/ready" >/dev/null; then
  echo "prometheus did not become ready" >&2
  cat "${temporary_directory}/prometheus-port-forward.log" >&2
  exit 1
fi

python3 - "${BASE_URL}" "${REQUESTS}" "${MIN_WORKERS}" "${REQUEST_TIMEOUT_SECONDS}" "${temporary_directory}/request-id" <<'PY'
import json
import sys
import urllib.request

base_url = sys.argv[1]
request_count = int(sys.argv[2])
minimum_workers = int(sys.argv[3])
request_timeout_seconds = float(sys.argv[4])
request_id_path = sys.argv[5]
payload = json.dumps({
    "model": "mock-qwen",
    "messages": [{"role": "user", "content": "E2E pipeline test"}],
    "max_tokens": 8,
    "temperature": 0,
}).encode()
workers = set()

for request_number in range(1, request_count + 1):
    request = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=request_timeout_seconds) as response:
        if response.status != 200:
            raise AssertionError(f"request {request_number}: status {response.status}, want 200")
        request_id = response.headers.get("X-Request-ID")
        if not request_id:
            raise AssertionError(f"request {request_number}: missing X-Request-ID response header")
        if request_number == 1:
            with open(request_id_path, "w", encoding="utf-8") as request_id_file:
                request_id_file.write(request_id)
        body = json.load(response)

    if body.get("object") != "chat.completion":
        raise AssertionError(f"request {request_number}: invalid object: {body.get('object')!r}")
    if body.get("model") != "mock-qwen":
        raise AssertionError(f"request {request_number}: invalid model: {body.get('model')!r}")

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise AssertionError(f"request {request_number}: invalid choices structure") from error

    if not isinstance(content, str) or not content:
        raise AssertionError(f"request {request_number}: response content is empty")

    prefix = "Hello from "
    if content.startswith(prefix):
        workers.add(content.removeprefix(prefix))

if len(workers) < minimum_workers:
    raise AssertionError(
        f"requests reached {len(workers)} worker(s), expected at least {minimum_workers}: "
        f"{', '.join(sorted(workers))}"
    )

summary = f"Pipeline OK: {request_count} request(s)"
if workers:
    summary += f", workers: {', '.join(sorted(workers))}"
print(summary)
PY

for _ in {1..30}; do
  if ! kill -0 "${loki_port_forward_pid}" >/dev/null 2>&1; then
    cat "${temporary_directory}/loki-port-forward.log" >&2
    exit 1
  fi
  if curl --fail --silent --show-error "${LOKI_BASE_URL}/ready" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if [[ "${AUTOSCALING}" != "1" ]]; then
  python3 - "${LOKI_BASE_URL}" "${temporary_directory}/request-id" <<'PY'
import json
import sys
import time
import urllib.parse
import urllib.request

base_url = sys.argv[1]
with open(sys.argv[2], encoding="utf-8") as request_id_file:
    request_id = request_id_file.read()
query = f'{{namespace="inference"}} |= "{request_id}"'

for _ in range(30):
    parameters = urllib.parse.urlencode({
        "query": query,
        "start": str(time.time_ns() - 300_000_000_000),
        "end": str(time.time_ns()),
        "limit": "100",
    })
    with urllib.request.urlopen(f"{base_url}/loki/api/v1/query_range?{parameters}", timeout=5) as response:
        payload = json.load(response)

    events = set()
    for stream in payload.get("data", {}).get("result", []):
        for _, line in stream.get("values", []):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("request_id") == request_id:
                events.add((record.get("component"), record.get("event")))

    expected = {("gateway", "request_completed"), ("worker", "request_completed")}
    if expected <= events:
        print("Loki correlation OK: gateway and worker logs share request ID")
        break
    time.sleep(2)
else:
    raise AssertionError(f"Loki did not return correlated request logs; observed {sorted(events)}")
PY
fi

wait_for_prometheus_metric() {
  local metric_name="$1"
  local query="$2"

  for _ in {1..30}; do
    if python3 - "${PROMETHEUS_BASE_URL}" "${query}" 2>/dev/null <<'PY'
import json
import math
import sys
import urllib.parse
import urllib.request

base_url = sys.argv[1]
query = urllib.parse.urlencode({"query": sys.argv[2]})

with urllib.request.urlopen(f"{base_url}/api/v1/query?{query}", timeout=5) as response:
    payload = json.load(response)

results = payload.get("data", {}).get("result", [])
values = [float(result["value"][1]) for result in results]
if payload.get("status") != "success" or not any(math.isfinite(value) and value > 0 for value in values):
    raise SystemExit(1)
PY
    then
      echo "Prometheus metric OK: ${metric_name}"
      return 0
    fi
    sleep 2
  done

  echo "prometheus did not collect a positive sample for ${metric_name}" >&2
  return 1
}

wait_for_prometheus_metric \
  "inference-gateway scrape target" \
  "sum(up{namespace=\"${NAMESPACE}\",service=\"inference-gateway\"})"
wait_for_prometheus_metric \
  "gateway_http_requests_total" \
  'sum(gateway_http_requests_total{route="/v1/chat/completions"})'
wait_for_prometheus_metric \
  "vllm-router scrape target" \
  "sum(up{namespace=\"${NAMESPACE}\",service=\"vllm-router\"})"
wait_for_prometheus_metric \
  "vllm_router_active_workers" \
  'sum(vllm_router_active_workers)'

echo "Observability OK: gateway and router metrics are available in Prometheus"

if [[ "${AUTOSCALING}" == "1" ]]; then
  wait_for_prometheus_metric "vLLM scrape target" \
    'sum(up{namespace="inference",service="mock-vllm"})'
  helm upgrade --install keda keda \
    --repo https://kedacore.github.io/charts \
    --version 2.20.0 \
    --namespace keda \
    --create-namespace \
    --values "${REPOSITORY_ROOT}/deploy/platform/keda/keda-values-local.yaml" \
    --wait --timeout 5m
  kubectl wait --for=condition=Established crd/scaledobjects.keda.sh --timeout=60s
  kubectl apply -k "${REPOSITORY_ROOT}/deploy/kubernetes/components/autoscaling"
  kubectl wait -n "${NAMESPACE}" --for=condition=Ready scaledobject/vllm-worker --timeout=120s
  hpa_name="$(kubectl get scaledobject vllm-worker -n "${NAMESPACE}" -o jsonpath='{.status.hpaName}')"
  kubectl wait -n "${NAMESPACE}" --for=condition=ScalingActive "hpa/${hpa_name}" --timeout=120s

  kubectl port-forward -n "${NAMESPACE}" service/vllm-router "${ROUTER_LOCAL_PORT}:30000" \
    >"${temporary_directory}/router-port-forward.log" 2>&1 &
  router_port_forward_pid=$!
  for _ in {1..30}; do
    if curl --fail --silent "http://127.0.0.1:${ROUTER_LOCAL_PORT}/health" >/dev/null; then
      break
    fi
    sleep 1
  done
  curl --fail --silent --show-error "http://127.0.0.1:${ROUTER_LOCAL_PORT}/health" >/dev/null
  python3 "${SCRIPT_DIRECTORY}/autoscaling.py" \
    "${BASE_URL}" "http://127.0.0.1:${ROUTER_LOCAL_PORT}" "${PROMETHEUS_BASE_URL}" \
    "${temporary_directory}" &
  autoscaling_test_pid=$!
  wait "${autoscaling_test_pid}"
  autoscaling_test_pid=""
fi
