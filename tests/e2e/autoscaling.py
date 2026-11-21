import concurrent.futures
import json
import math
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


gateway_url, router_url, prometheus_url, output_directory = sys.argv[1:]
output_directory = Path(output_directory)
stop = threading.Event()
errors = []
lock = threading.Lock()


def kubectl(*args):
    return json.loads(subprocess.check_output(
        ["kubectl", "-n", "inference", *args, "-o", "json"], timeout=15
    ))


def query(expression):
    url = prometheus_url + "/api/v1/query?" + urllib.parse.urlencode({"query": expression})
    with urllib.request.urlopen(url, timeout=10) as response:
        result = json.load(response)
    if result.get("status") != "success":
        raise AssertionError(f"Prometheus query failed: {result}")
    values = result["data"]["result"]
    if not values or any(not math.isfinite(float(item["value"][1])) for item in values):
        raise AssertionError(f"Prometheus returned missing/non-finite data: {expression}")
    return values


def scalar(expression):
    values = query(expression)
    if len(values) != 1:
        raise AssertionError(f"Expected one metric: {values}")
    return float(values[0]["value"][1])


def request(base_url, tokens, force_length=False):
    payload = {
        "model": "mock-qwen",
        "messages": [{"role": "user", "content": "Explain a queue."}],
        "temperature": 0,
        "max_tokens": tokens,
    }
    if force_length:
        payload.update(min_tokens=tokens, ignore_eos=True)
    req = urllib.request.Request(
        base_url + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        body = json.load(response)
        if response.status != 200 or not body["choices"][0]["message"]["content"]:
            raise AssertionError(f"Invalid completion: {body}")
        if force_length and body["usage"]["completion_tokens"] != tokens:
            raise AssertionError(f"Expected {tokens} generated tokens: {body['usage']}")
    return body["usage"]["completion_tokens"]


def load_worker(worker):
    with (output_directory / f"requests-{worker}.jsonl").open("w") as log:
        while not stop.is_set():
            started = time.time()
            record = {"started": started, "worker": worker}
            try:
                record.update(status=200, tokens=request(router_url, 512, True))
            except Exception as error:
                record.update(status=getattr(error, "code", None), error=str(error))
                with lock:
                    errors.append(record)
                stop.set()
            record["duration"] = time.time() - started
            log.write(json.dumps(record) + "\n")
            log.flush()


def sample(metric_query):
    deployment = kubectl("get", "deployment", "mock-vllm")
    pods = kubectl("get", "pods", "-l", "app=mock-vllm")["items"]
    active_pods = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]
    for pod in pods:
        for container in pod["status"].get("containerStatuses", []):
            if container.get("restartCount", 0) or container.get("lastState", {}).get("terminated", {}).get("reason") == "OOMKilled":
                raise AssertionError(f"Worker restarted: {pod['metadata']['name']}: {container}")
    desired = deployment["spec"]["replicas"]
    if not 1 <= desired <= 3:
        raise AssertionError(f"Replica count outside bounds: {desired}")
    scaled_object = kubectl("get", "scaledobject", "vllm-worker")
    if not any(item["type"] == "Ready" and item["status"] == "True"
               for item in scaled_object.get("status", {}).get("conditions", [])):
        raise AssertionError(f"ScaledObject unhealthy: {scaled_object.get('status')}")
    hpa_name = scaled_object["status"]["hpaName"]
    hpa = kubectl("get", "hpa", hpa_name)
    conditions = {item["type"]: item["status"] for item in hpa.get("status", {}).get("conditions", [])}
    if conditions.get("AbleToScale") != "True" or conditions.get("ScalingActive") != "True":
        raise AssertionError(f"HPA unhealthy: {hpa.get('status')}")
    successes = query('sum by(pod) (vllm:request_success_total{namespace="inference",service="mock-vllm",model_name="mock-qwen",finished_reason="length"})')
    return {
        "time": time.time(),
        "desired": desired,
        "ready": deployment["status"].get("readyReplicas", 0),
        "pods": [pod["metadata"]["name"] for pod in active_pods],
        "load": scalar(metric_query),
        "router_workers": scalar('max(llm_d_epp_ready_endpoints{namespace="inference",service="llmd-epp",name="llmd"})'),
        "up": scalar('sum(up{namespace="inference",service="mock-vllm"})'),
        "successes": {item["metric"]["pod"]: float(item["value"][1]) for item in successes},
    }


def interrupted(signum, frame):
    stop.set()
    raise KeyboardInterrupt


def main():
    scaled_object = kubectl("get", "scaledobject", "vllm-worker")
    metric_query = scaled_object["spec"]["triggers"][0]["metadata"]["query"]
    with (output_directory / "timeline.jsonl").open("w") as timeline:
        def observe():
            value = sample(metric_query)
            timeline.write(json.dumps(value) + "\n")
            timeline.flush()
            print(f"replicas={value['desired']} ready={value['ready']} router={value['router_workers']} load={value['load']}", flush=True)
            with lock:
                if errors:
                    raise AssertionError(f"Load request failed: {errors[0]}")
            return value

        request(gateway_url, 8)
        time.sleep(6)
        baseline = observe()
        if baseline["desired"] != 1 or baseline["ready"] != 1 or baseline["up"] != 1:
            raise AssertionError(f"Expected one healthy initial worker: {baseline}")
        seen = dict(baseline["successes"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(load_worker, worker) for worker in range(6)]
            try:
                deadline = time.monotonic() + 600
                while time.monotonic() < deadline:
                    value = observe()
                    for pod, count in value["successes"].items():
                        seen.setdefault(pod, max(count, 1))
                    if value["desired"] == value["ready"] == value["router_workers"] == value["up"] == 3:
                        if len(value["pods"]) == 3 and all(
                            value["successes"].get(pod, 0) > seen.get(pod, math.inf)
                            for pod in value["pods"]
                        ):
                            print("Scale-up OK: three healthy workers served traffic", flush=True)
                            break
                    time.sleep(5)
                else:
                    raise AssertionError("Scale-up/routing did not reach three workers within 10 minutes")
            finally:
                stop.set()
                for future in futures:
                    future.result()
        with lock:
            if errors:
                raise AssertionError(f"Load request failed: {errors[0]}")
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            value = observe()
            if value["desired"] == value["ready"] == value["router_workers"] == value["up"] == 1 and len(value["pods"]) == 1:
                request(gateway_url, 8)
                print("Autoscaling OK: 1 -> 3 -> 1, final gateway smoke passed", flush=True)
                return
            time.sleep(5)
        raise AssertionError("Scale-down did not return to one worker within 10 minutes")


signal.signal(signal.SIGTERM, interrupted)
try:
    main()
finally:
    stop.set()
