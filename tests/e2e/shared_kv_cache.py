import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import subprocess
import time
import uuid

from external_kv_cache import get_json, http, identity, kubectl, metric


def ready_pods(label, count):
    pods = get_json("get", "pods", "-l", label)["items"]
    pods = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]
    if len(pods) != count:
        raise RuntimeError(f"Expected {count} Pods for {label}, found {len(pods)}")
    for pod in pods:
        statuses = pod["status"].get("containerStatuses", [])
        if not statuses or not all(status["ready"] for status in statuses):
            raise RuntimeError(f"Pod {pod['metadata']['name']} is not ready")
    return sorted(pods, key=lambda pod: pod["metadata"]["name"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    forwards = []
    results = []
    run_id = uuid.uuid4().hex

    def save(name, value):
        with (args.output / name).open("x") as output:
            output.write(value if isinstance(value, str) else json.dumps(value, indent=2))

    def wait_for(description, check, timeout=60):
        deadline = time.monotonic() + timeout
        while True:
            value = check()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise TimeoutError(description)
            time.sleep(0.5)

    def forward(target, remote_port):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with (args.output / f"forward-{len(forwards)}.log").open("x") as log:
            process = subprocess.Popen(
                ["kubectl", "-n", "inference", "port-forward", target,
                 f"{port}:{remote_port}"], stdout=log, stderr=subprocess.STDOUT
            )
        forwards.append(process)

        def connected():
            if process.poll() is not None:
                raise RuntimeError(f"Port-forward failed for {target}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    return True
            except OSError:
                return False

        wait_for(f"Port-forward did not start for {target}", connected, 30)
        return f"http://127.0.0.1:{port}"

    def completion(url, request):
        response = json.loads(http(url, "/v1/completions", request))
        if response["usage"]["prompt_tokens"] != len(request["prompt"]):
            raise RuntimeError("Unexpected prompt length")
        if response["usage"]["completion_tokens"] != request["max_tokens"]:
            raise RuntimeError("Generation did not complete")
        return response

    try:
        if kubectl("config", "current-context").strip() != "kind-inference-lab":
            raise RuntimeError("Test requires the local kind-inference-lab context")
        resources = kubectl("api-resources", "--api-group=keda.sh", "-o", "name")
        if "scaledobjects.keda.sh" in resources and get_json("get", "scaledobjects")["items"]:
            raise RuntimeError("Disable autoscaling before running this test")
        if get_json("get", "hpa")["items"]:
            raise RuntimeError("Remove active HPA before running this test")
        deployment = get_json("get", "deployment", "mock-vllm")
        if deployment["spec"]["replicas"] != 2:
            raise RuntimeError("Test requires exactly two worker replicas")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        if "--no-enable-prefix-caching" not in container.get("args", []):
            raise RuntimeError("Disable local prefix caching to isolate external sharing")
        save("worker-deployment.json", deployment)
        pods = ready_pods("app=mock-vllm", 2)
        cache = ready_pods("app=kv-cache", 1)[0]
        if len({pod["spec"]["nodeName"] for pod in [*pods, cache]}) != 1:
            raise RuntimeError("This profile requires one shared Kubernetes node")
        if len({pod["status"]["containerStatuses"][0]["imageID"] for pod in pods}) != 1:
            raise RuntimeError("Workers must use the same image digest")
        save("workers-before.json", pods)
        save("cache-before.json", cache)
        cache_url = forward("service/kv-cache", 8080)
        urls = [forward(f"pod/{pod['metadata']['name']}", 8000) for pod in pods]

        def prompt(label, length=512):
            text = f"Experiment {run_id} {label}. " + (
                "The researcher records measurements of the coastal weather every morning. " * 100
            )
            tokens = json.loads(http(urls[0], "/tokenize", {
                "model": "mock-qwen", "prompt": text,
            }))["tokens"][:length]
            if len(tokens) != length:
                raise RuntimeError(f"Expected {length} prompt tokens")
            return {"model": "mock-qwen", "prompt": tokens, "temperature": 0,
                    "max_tokens": 32, "ignore_eos": True, "seed": 0}

        def sample():
            cache_text = http(cache_url, "/metrics")
            worker_texts = [http(url, "/metrics") for url in urls]
            return cache_text, worker_texts

        def counters(raw):
            cache_text, worker_texts = raw
            return {
                "writes": metric(cache_text, "lmcache_mp_l1_write_chunks_total"),
                "reads": metric(cache_text, "lmcache_mp_l1_read_chunks_total"),
                "workers": [{
                    "computed": metric(text, "vllm:request_prefill_kv_computed_tokens_sum"),
                    "hits": metric(text, "vllm:external_prefix_cache_hits_total"),
                    "success": metric(text, "vllm:request_success_total"),
                } for text in worker_texts],
            }

        def snapshot(label):
            raw = sample()
            save(f"{label}-cache.prom", raw[0])
            for index, text in enumerate(raw[1]):
                save(f"{label}-worker-{index}.prom", text)
            return counters(raw)

        # Bootstrap only: OTel creates series lazily; measured scenarios require them.
        def bootstrap(index, label):
            request = prompt(label, 256)
            request["max_tokens"] = 1
            deadline = time.monotonic() + 60
            attempt = 0
            while True:
                response = completion(urls[index], request)
                save(f"{label}-{attempt}.json", response)
                attempt += 1
                raw = sample()
                text = raw[1][index]
                try:
                    hit = metric(text, "vllm:external_prefix_cache_hits_total")
                    reads = metric(raw[0], "lmcache_mp_l1_read_chunks_total")
                    writes = metric(raw[0], "lmcache_mp_l1_write_chunks_total")
                    if attempt >= 2 and hit > 0 and reads > 0 and writes > 0:
                        return
                except RuntimeError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Cache counters were not initialized for {label}")
                time.sleep(0.5)

        bootstrap(0, "bootstrap-a")
        bootstrap(1, "bootstrap-b")
        baseline = snapshot("idle-before")
        time.sleep(5)
        if snapshot("idle-after") != baseline:
            raise RuntimeError("Foreign traffic or unsettled cache operations detected")

        def run(label, calls, warm=False, expected=None):
            save(f"{label}-requests.json", [request for _, request in calls])
            before = snapshot(f"{label}-before")
            with ThreadPoolExecutor(max_workers=len(calls)) as pool:
                futures = [pool.submit(completion, urls[index], request) for index, request in calls]
                responses = [future.result() for future in futures]
            save(f"{label}-responses.json", responses)
            expected_counts = [sum(index == worker for index, _ in calls) for worker in range(2)]
            poll = 0

            def observed():
                nonlocal poll
                after = snapshot(f"{label}-poll-{poll}")
                poll += 1
                successes = [after["workers"][i]["success"] - before["workers"][i]["success"]
                             for i in range(2)]
                if any(value > expected_counts[i] for i, value in enumerate(successes)):
                    raise RuntimeError("Unexpected request count: foreign traffic detected")
                if successes != expected_counts:
                    return None
                if warm:
                    for index, _ in calls:
                        if after["workers"][index]["hits"] - before["workers"][index]["hits"] < 128:
                            return None
                    if after["reads"] <= before["reads"]:
                        return None
                elif after["writes"] <= before["writes"]:
                    return None
                return after

            after = wait_for(f"Expected cache operations missing for {label}", observed)
            deltas = [{key: after["workers"][i][key] - before["workers"][i][key]
                       for key in ("computed", "hits", "success")} for i in range(2)]
            for index, request in calls:
                computed = deltas[index]["computed"]
                if warm and computed > len(request["prompt"]) - 128:
                    raise RuntimeError(f"Cached prefill was recomputed for {label}: {deltas}")
                if not warm and (computed != len(request["prompt"]) or deltas[index]["hits"] != 0):
                    raise RuntimeError(f"Expected a cold prefix for {label}: {deltas}")
            if expected is not None:
                for response, text in zip(responses, expected, strict=True):
                    if response["choices"][0]["text"] != text:
                        raise RuntimeError(f"Cached and uncached outputs differ for {label}")
            results.append({"scenario": label, "workers": deltas,
                            "reads": after["reads"] - before["reads"],
                            "writes": after["writes"] - before["writes"]})
            return [response["choices"][0]["text"] for response in responses]

        request_a = prompt("a-to-b")
        output_a = run("a-cold", [(0, request_a)])
        run("b-reuses-a", [(1, request_a)], warm=True, expected=output_a)
        request_b = prompt("b-to-a")
        output_b = run("b-cold", [(1, request_b)])
        run("a-reuses-b", [(0, request_b)], warm=True, expected=output_b)
        run("parallel-distinct-prefixes", [(0, request_b), (1, request_a)],
            warm=True, expected=[output_b[0], output_a[0]])
        run("parallel-shared-prefix", [(0, request_a), (1, request_a)],
            warm=True, expected=[output_a[0], output_a[0]])

        continuation = {**request_a, "prompt": request_a["prompt"] + request_b["prompt"][:64]}
        continued_output = run("a-extends-prefix", [(0, continuation)], warm=True)
        run("b-continues-same-context", [(1, continuation)], warm=True, expected=continued_output)
        router_url = forward("service/vllm-router", 30000)
        router_response = completion(router_url, request_a)
        save("router-response.json", router_response)
        if router_response["choices"][0]["text"] != output_a[0]:
            raise RuntimeError("Router response differs from direct worker output")

        current = ready_pods("app=mock-vllm", 2)
        if [identity(pod) for pod in current] != [identity(pod) for pod in pods]:
            raise RuntimeError("Worker identity or restart count changed during sharing tests")
        removed = pods[0]["metadata"]["name"]
        kubectl("delete", "pod", removed, "--wait=true", "--timeout=60s")
        survivor_response = completion(urls[1], request_a)
        save("survivor-response.json", survivor_response)
        if survivor_response["choices"][0]["text"] != output_a[0]:
            raise RuntimeError("Surviving worker returned a different output")

        def replacement_ready():
            candidates = get_json("get", "pods", "-l", "app=mock-vllm")["items"]
            candidates = [pod for pod in candidates
                          if not pod["metadata"].get("deletionTimestamp")]
            if len(candidates) != 2:
                return None
            for pod in candidates:
                statuses = pod["status"].get("containerStatuses", [])
                if not statuses or not all(status["ready"] for status in statuses):
                    return None
            return next((pod for pod in candidates
                         if pod["metadata"]["uid"] not in {p["metadata"]["uid"] for p in pods}), None)

        replacement = wait_for("Replacement worker did not become ready", replacement_ready, 900)
        save("replacement.json", replacement)
        if any(identity(replacement)[1]):
            raise RuntimeError("Replacement worker has restarted")
        urls[0] = forward(f"pod/{replacement['metadata']['name']}", 8000)
        bootstrap(0, "bootstrap-replacement")
        run("replacement-reuses-b", [(0, request_b)], warm=True, expected=output_b)
        final_pods = ready_pods("app=mock-vllm", 2)
        survivor = next(pod for pod in final_pods if pod["metadata"]["name"] == pods[1]["metadata"]["name"])
        if identity(survivor) != identity(pods[1]):
            raise RuntimeError("Surviving worker changed or restarted")
        final_replacement = next(pod for pod in final_pods
                                 if pod["metadata"]["uid"] == replacement["metadata"]["uid"])
        if identity(final_replacement) != identity(replacement):
            raise RuntimeError("Replacement worker changed or restarted during verification")
        if identity(ready_pods("app=kv-cache", 1)[0]) != identity(cache):
            raise RuntimeError("Cache Pod changed or restarted")
        save("workers-after.json", final_pods)
        save("summary.json", {"valid": True, "run_id": run_id, "scenarios": results})
        print(json.dumps({"valid": True, "scenarios": len(results)}, indent=2))
    except Exception as error:
        save("failure.txt", str(error))
        for label in ("app=mock-vllm", "app=kv-cache"):
            logs = subprocess.run(
                ["kubectl", "-n", "inference", "logs", "-l", label,
                 "--all-containers", "--prefix", "--tail=200"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            save(f"{label.split('=')[1]}.log", logs.stdout)
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
