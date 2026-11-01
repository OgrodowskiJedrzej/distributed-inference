#!/usr/bin/env bash

set -euo pipefail

NAMESPACE="${E2E_NAMESPACE:-inference}"
KIND_CLUSTER="${E2E_KIND_CLUSTER:-inference-lab}"
GATEWAY_IMAGE="${E2E_GATEWAY_IMAGE:-inference-gateway:e2e}"
LOCAL_PORT="${E2E_PORT:-18000}"
PROMETHEUS_LOCAL_PORT="${E2E_PROMETHEUS_PORT:-19090}"
REQUESTS="${E2E_REQUESTS:-3}"
MIN_WORKERS="${E2E_MIN_WORKERS:-0}"
REQUEST_TIMEOUT_SECONDS="${E2E_REQUEST_TIMEOUT_SECONDS:-120}"
MONITORING_NAMESPACE="${E2E_MONITORING_NAMESPACE:-monitoring}"
MONITORING_RELEASE="${E2E_MONITORING_RELEASE:-monitoring}"
MONITORING_CHART_VERSION="${E2E_MONITORING_CHART_VERSION:-92.16.1}"
MONITORING_HELM_TIMEOUT="${E2E_MONITORING_HELM_TIMEOUT:-10m}"
BASE_URL="http://127.0.0.1:${LOCAL_PORT}"
PROMETHEUS_BASE_URL="http://127.0.0.1:${PROMETHEUS_LOCAL_PORT}"
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

temporary_directory="$(mktemp -d)"
gateway_port_forward_pid=""
prometheus_port_forward_pid=""

cleanup() {
  for pid in "${gateway_port_forward_pid}" "${prometheus_port_forward_pid}"; do
    if [[ -n "${pid}" ]]; then
      kill "${pid}" >/dev/null 2>&1 || true
      wait "${pid}" >/dev/null 2>&1 || true
    fi
  done
  rm -f "${temporary_directory}/gateway-port-forward.log"
  rm -f "${temporary_directory}/prometheus-port-forward.log"
  rmdir "${temporary_directory}"
}
trap cleanup EXIT

echo "Building and deploying gateway..."
docker build \
  --tag "${GATEWAY_IMAGE}" \
  "${REPOSITORY_ROOT}/gateway"
kind load docker-image \
  "${GATEWAY_IMAGE}" \
  --name "${KIND_CLUSTER}"

kubectl apply -k "${REPOSITORY_ROOT}/deployment/kubernetes"
kubectl set image \
  deployment/inference-gateway \
  gateway="${GATEWAY_IMAGE}" \
  -n "${NAMESPACE}"

echo "Checking Kubernetes workloads..."
kubectl rollout status deployment/mock-vllm -n "${NAMESPACE}" --timeout=60s
kubectl rollout status deployment/vllm-router -n "${NAMESPACE}" --timeout=60s
kubectl rollout status deployment/inference-gateway -n "${NAMESPACE}" --timeout=60s

echo "Deploying observability stack..."
helm upgrade --install "${MONITORING_RELEASE}" \
  oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack \
  --version "${MONITORING_CHART_VERSION}" \
  --namespace "${MONITORING_NAMESPACE}" \
  --create-namespace \
  --values "${REPOSITORY_ROOT}/deployment/observability/values-local.yaml" \
  --wait \
  --timeout "${MONITORING_HELM_TIMEOUT}"

kubectl apply -f "${REPOSITORY_ROOT}/deployment/observability/monitors"

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

python3 - "${BASE_URL}" "${REQUESTS}" "${MIN_WORKERS}" "${REQUEST_TIMEOUT_SECONDS}" <<'PY'
import json
import sys
import urllib.request

base_url = sys.argv[1]
request_count = int(sys.argv[2])
minimum_workers = int(sys.argv[3])
request_timeout_seconds = float(sys.argv[4])
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
