import argparse
import json
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.request
import uuid


def kubectl(*args):
    return subprocess.check_output(
        ["kubectl", "-n", "inference", *args], text=True
    )


def get_json(*args):
    return json.loads(kubectl(*args, "-o", "json"))


def one_pod(label):
    pods = get_json("get", "pods", "-l", label)["items"]
    pods = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
    if len(pods) != 1:
        raise RuntimeError(f"Expected one Pod for {label}, found {len(pods)}")
    pod = pods[0]
    statuses = pod["status"].get("containerStatuses", [])
    if not statuses or not all(s["ready"] for s in statuses):
        raise RuntimeError(f"Pod {pod['metadata']['name']} is not ready")
    return pod


def identity(pod):
    return (
        pod["metadata"]["uid"],
        [s["restartCount"] for s in pod["status"]["containerStatuses"]],
    )


def http(base, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read().decode()


def metric(text, name):
    values = re.findall(
        rf"^{re.escape(name)}(?:\{{[^\n]*\}})?\s+([^\s]+)", text, re.MULTILINE
    )
    if not values:
        raise RuntimeError(f"Required metric missing: {name}")
    return sum(float(value) for value in values)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    forwards = []

    def save(name, value):
        path = args.output / name
        path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2))

    def forward(target, remote_port):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        log = (args.output / f"forward-{port}.log").open("w")
        process = subprocess.Popen(
            ["kubectl", "-n", "inference", "port-forward", target,
             f"{port}:{remote_port}"], stdout=log, stderr=subprocess.STDOUT
        )
        log.close()
        forwards.append(process)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Port-forward failed for {target}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    return f"http://127.0.0.1:{port}"
            except OSError:
                time.sleep(0.2)
        raise TimeoutError(f"Port-forward did not start for {target}")

    try:
        keda_resources = kubectl("api-resources", "--api-group=keda.sh", "-o", "name")
        if "scaledobjects.keda.sh" in keda_resources and get_json("get", "scaledobjects")["items"]:
            raise RuntimeError("Disable autoscaling before running this test")
        if get_json("get", "hpa")["items"]:
            raise RuntimeError("Remove active HPA before running this test")
        deployment = get_json("get", "deployment", "mock-vllm")
        if deployment["spec"]["replicas"] != 1:
            raise RuntimeError("Test requires exactly one worker replica")
        save("worker-deployment.json", deployment)
        cache = one_pod("app=kv-cache")
        worker = one_pod("app=mock-vllm")
        save("cache-before.json", cache)
        save("worker-before.json", worker)
        cache_id = identity(cache)
        cache_url = forward("service/kv-cache", 8080)
        router_url = forward("service/vllm-router", 30000)

        def snapshot(label, worker_url):
            cache_metrics = http(cache_url, "/metrics")
            worker_metrics = http(worker_url, "/metrics")
            save(f"{label}-cache.prom", cache_metrics)
            save(f"{label}-worker.prom", worker_metrics)
            return {
                "writes": metric(cache_metrics, "lmcache_mp_l1_write_chunks_total"),
                "reads": metric(cache_metrics, "lmcache_mp_l1_read_chunks_total"),
                "computed": metric(worker_metrics, "vllm:request_prefill_kv_computed_tokens_sum"),
                "hits": metric(worker_metrics, "vllm:external_prefix_cache_hits_total"),
                "success": metric(worker_metrics, "vllm:request_success_total"),
            }

        worker_url = forward(f"pod/{worker['metadata']['name']}", 8000)
        # OTel creates cache counter series lazily. Exercise both paths using
        # an unrelated prefix instead of treating absent counters as zero.
        bootstrap_tokens = json.loads(http(worker_url, "/tokenize", {
            "model": "mock-qwen", "prompt": f"Initialize {uuid.uuid4()}. " + "Weather observations. " * 100
        }))["tokens"][:256]
        for index in range(2):
            save(f"bootstrap-{index}.json", json.loads(http(router_url, "/v1/completions", {
                "model": "mock-qwen", "prompt": bootstrap_tokens,
                "max_tokens": 1, "temperature": 0
            })))
            time.sleep(3)
        idle_before = http(worker_url, "/metrics")
        time.sleep(5)
        idle_after = http(worker_url, "/metrics")
        for name in ("vllm:prompt_tokens_total", "vllm:generation_tokens_total"):
            if metric(idle_after, name) != metric(idle_before, name):
                raise RuntimeError("Foreign traffic detected; stop other clients")
        for name in ("vllm:num_requests_running", "vllm:num_requests_waiting"):
            if metric(idle_after, name) != 0:
                raise RuntimeError("Worker is not idle")

        prompt = f"Experiment {uuid.uuid4()}. " + (
            "The researcher records measurements of the coastal weather every morning. " * 100
        )
        tokens = json.loads(http(worker_url, "/tokenize", {
            "model": "mock-qwen", "prompt": prompt
        }))["tokens"][:512]
        if len(tokens) != 512:
            raise RuntimeError("Expected a 512-token prompt")
        request = {"model": "mock-qwen", "prompt": tokens, "temperature": 0,
                   "max_tokens": 32, "ignore_eos": True, "seed": 0}
        save("request.json", request)

        def generate(label, url):
            before = snapshot(f"{label}-before", url)
            response = json.loads(http(router_url, "/v1/completions", request))
            save(f"{label}-response.json", response)
            if response["usage"]["prompt_tokens"] != 512:
                raise RuntimeError("Unexpected prompt length")
            if response["usage"]["completion_tokens"] != 32:
                raise RuntimeError("Generation did not complete")
            time.sleep(3)
            after = snapshot(f"{label}-after", url)
            delta = {key: after[key] - before[key] for key in before}
            if delta["success"] != 1:
                raise RuntimeError("Unexpected request count: foreign traffic or failed generation")
            return response, delta

        cold, cold_delta = generate("cold", worker_url)
        if cold_delta["writes"] < 1 or cold_delta["computed"] != 512:
            raise RuntimeError(f"Cold request did not populate cache: {cold_delta}")
        if identity(one_pod("app=mock-vllm")) != identity(worker):
            raise RuntimeError("Worker restarted during cold request")

        kubectl("rollout", "restart", "deployment/mock-vllm")
        kubectl("rollout", "status", "deployment/mock-vllm", "--timeout=15m")
        replacement = one_pod("app=mock-vllm")
        if replacement["metadata"]["uid"] == worker["metadata"]["uid"]:
            raise RuntimeError("Worker was not replaced")
        replacement_id = identity(replacement)
        if any(replacement_id[1]):
            raise RuntimeError("Replacement worker has restarted")
        save("worker-after.json", replacement)
        worker_url = forward(f"pod/{replacement['metadata']['name']}", 8000)
        warm, warm_delta = generate("warm", worker_url)
        if warm_delta["reads"] < 1 or warm_delta["hits"] < 128:
            raise RuntimeError(f"No external cache hit after replacement: {warm_delta}")
        if cold_delta["computed"] - warm_delta["computed"] < 128:
            raise RuntimeError(f"Cached prefill was recomputed: {warm_delta}")
        if cold["choices"][0]["text"] != warm["choices"][0]["text"]:
            raise RuntimeError("Cached and uncached generation differ")
        if identity(one_pod("app=kv-cache")) != cache_id:
            raise RuntimeError("Cache Pod changed or restarted")
        if identity(one_pod("app=mock-vllm")) != replacement_id:
            raise RuntimeError("Replacement worker restarted during generation")
        save("summary.json", {"valid": True, "cold": cold_delta, "warm": warm_delta})
        print(json.dumps({"cold": cold_delta, "warm": warm_delta}, indent=2))
    except Exception as error:
        save("failure.txt", str(error))
        for target in ("deployment/mock-vllm", "deployment/kv-cache"):
            result = subprocess.run(
                ["kubectl", "-n", "inference", "logs", target, "--tail=200"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            save(target.replace("/", "-") + ".log", result.stdout)
        raise
    finally:
        for process in forwards:
            process.terminate()
        for process in forwards:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
