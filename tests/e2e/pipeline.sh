#!/usr/bin/env bash

set -euo pipefail

NAMESPACE="${E2E_NAMESPACE:-inference}"
LOCAL_PORT="${E2E_PORT:-18000}"
REQUESTS="${E2E_REQUESTS:-3}"
MIN_WORKERS="${E2E_MIN_WORKERS:-1}"
BASE_URL="http://127.0.0.1:${LOCAL_PORT}"

for command in kubectl curl python3; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "missing required command: ${command}" >&2
    exit 1
  fi
done

temporary_directory="$(mktemp -d)"
port_forward_pid=""

cleanup() {
  if [[ -n "${port_forward_pid}" ]]; then
    kill "${port_forward_pid}" >/dev/null 2>&1 || true
    wait "${port_forward_pid}" >/dev/null 2>&1 || true
  fi
  rm -f "${temporary_directory}/port-forward.log"
  rmdir "${temporary_directory}"
}
trap cleanup EXIT

echo "Checking Kubernetes workloads..."
kubectl rollout status deployment/mock-vllm -n "${NAMESPACE}" --timeout=60s
kubectl rollout status deployment/vllm-router -n "${NAMESPACE}" --timeout=60s
kubectl rollout status deployment/inference-gateway -n "${NAMESPACE}" --timeout=60s

kubectl port-forward \
  -n "${NAMESPACE}" \
  service/inference-gateway \
  "${LOCAL_PORT}:8000" \
  >"${temporary_directory}/port-forward.log" 2>&1 &
port_forward_pid=$!

for _ in {1..30}; do
  if ! kill -0 "${port_forward_pid}" >/dev/null 2>&1; then
    cat "${temporary_directory}/port-forward.log" >&2
    exit 1
  fi
  if curl --fail --silent --show-error "${BASE_URL}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl --fail --silent --show-error "${BASE_URL}/healthz" >/dev/null; then
  echo "gateway did not become healthy" >&2
  cat "${temporary_directory}/port-forward.log" >&2
  exit 1
fi

python3 - "${BASE_URL}" "${REQUESTS}" "${MIN_WORKERS}" <<'PY'
import json
import sys
import urllib.request

base_url = sys.argv[1]
request_count = int(sys.argv[2])
minimum_workers = int(sys.argv[3])
payload = json.dumps({
    "model": "mock-qwen",
    "messages": [{"role": "user", "content": "E2E pipeline test"}],
}).encode()
workers = set()

for request_number in range(1, request_count + 1):
    request = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
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

    prefix = "Hello from "
    if not content.startswith(prefix):
        raise AssertionError(f"request {request_number}: unexpected content: {content!r}")
    workers.add(content.removeprefix(prefix))

if len(workers) < minimum_workers:
    raise AssertionError(
        f"requests reached {len(workers)} worker(s), expected at least {minimum_workers}: "
        f"{', '.join(sorted(workers))}"
    )

print(f"Pipeline OK: {request_count} request(s), workers: {', '.join(sorted(workers))}")
PY
