import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.parse
import uuid

from client import validate


HERE = Path(__file__).resolve().parent
NAMESPACE = "inference"


def command(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30, **kwargs).stdout


def kubectl(*args):
    return command("kubectl", "-n", NAMESPACE, *args)


def get(resource, *args):
    return json.loads(kubectl("get", resource, *args, "-o", "json"))


def write_json(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)


def snapshot():
    resources = command("kubectl", "api-resources", "--api-group=keda.sh", "-o", "name").splitlines()
    return {"deployments": get("deployments"), "pods": get("pods"), "hpa": get("hpa"),
            "scaledobjects": get("scaledobjects.keda.sh") if "scaledobjects.keda.sh" in resources else {"items": []}}


def worker_config(state, config):
    deployment = next(item for item in state["deployments"]["items"] if item["metadata"]["name"] == "mock-vllm")
    worker = next(item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "worker")
    args = worker["args"]
    for flag, expected in (("--revision", config["model_revision"]), ("--served-model-name", config["served_model"]),
                           ("--max-model-len", str(config["max_model_len"]))):
        if flag not in args or args[args.index(flag) + 1] != expected:
            raise ValueError(f"Worker {flag} differs from the workload")
    if args[0] != config["model"]:
        raise ValueError("Worker model differs from the workload")
    pods = [pod for pod in state["pods"]["items"] if pod["metadata"].get("labels", {}).get("app") == "mock-vllm"
            and not pod["metadata"].get("deletionTimestamp")]
    ready = [(pod, status) for pod in pods for status in pod.get("status", {}).get("containerStatuses", [])
             if status["name"] == "worker" and status.get("ready")]
    if not ready:
        raise ValueError("No ready worker available")
    digests = {status["imageID"].removeprefix("docker-pullable://") for _, status in ready}
    if len(digests) != 1 or "@sha256:" not in next(iter(digests)):
        raise ValueError("Ready workers must share a pullable image digest")
    volumes = deployment["spec"]["template"]["spec"].get("volumes", [])
    mounts = worker.get("volumeMounts", [])
    cache_name = next(mount["name"] for mount in mounts if mount["mountPath"] == "/cache")
    claim = next(volume["persistentVolumeClaim"]["claimName"] for volume in volumes if volume["name"] == cache_name)
    return {"image": next(iter(digests)), "node": ready[0][0]["spec"]["nodeName"], "cache_claim": claim}


def gateway_policy(state, profile):
    if profile not in ("normal", "load"):
        raise ValueError("gateway_profile must be normal or load")
    pods = [pod for pod in state["pods"]["items"] if pod["metadata"].get("labels", {}).get("app") == "inference-gateway"
            and not pod["metadata"].get("deletionTimestamp")]
    if len(pods) != 1:
        raise ValueError("Expected exactly one gateway Pod")
    text = kubectl("exec", pods[0]["metadata"]["name"], "-c", "gateway", "--", "cat", "/app/config.yaml")
    keys = ("requests_per_minute", "tokens_per_minute", "max_concurrency")
    policy = {key: int(re.search(r"^\s*" + key + r":\s*(\d+)\s*$", text, re.MULTILINE)[1]) for key in keys}
    expected = (60, 100000, 8) if profile == "normal" else (6000, 10000000, 256)
    if tuple(policy[key] for key in keys) != expected:
        raise ValueError(f"Gateway limits do not match the declared {profile} profile")
    policy["reservation_ttl"] = re.search(r"^\s*reservation_ttl:\s*(\S+)", text, re.MULTILINE)[1]
    return policy


def pod_manifest(name, configmap, run_id, worker, config):
    model_path = config["model"].replace("/", "--")
    tokenizer = f"/cache/huggingface/hub/models--{model_path}/snapshots/{config['tokenizer_revision']}"
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": NAMESPACE,
                         "labels": {"app": "pipeline-benchmark", "run_id": run_id}},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False, "nodeName": worker["node"],
                     "containers": [{"name": "benchmark", "image": worker["image"], "imagePullPolicy": "IfNotPresent",
                                     "command": ["python3", "/scripts/client.py"],
                                     "args": ["--config", "/scripts/config.json", "--run-id", run_id, "--tokenizer", tokenizer],
                                     "env": [{"name": "HF_HUB_OFFLINE", "value": "1"}, {"name": "OMP_NUM_THREADS", "value": "1"}],
                                     "ports": [{"name": "metrics", "containerPort": 9091}],
                                     "resources": {"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {"cpu": "1", "memory": "2Gi"}},
                                     "volumeMounts": [{"name": "scripts", "mountPath": "/scripts", "readOnly": True},
                                                      {"name": "cache", "mountPath": "/cache", "readOnly": True},
                                                      {"name": "results", "mountPath": "/results"}]}],
                     "volumes": [{"name": "scripts", "configMap": {"name": configmap}},
                                 {"name": "cache", "persistentVolumeClaim": {"claimName": worker["cache_claim"], "readOnly": True}},
                                 {"name": "results", "emptyDir": {}}]}}


def dashboard_url(base, run_id, started, ended):
    query = urllib.parse.urlencode({"var-run_id": run_id, "from": int(started * 1000), "to": int(ended * 1000)})
    return base.rstrip("/") + "/d/inference-pipeline-experiment?" + query


def execute(args):
    config = json.loads(args.config.read_text())
    validate(config)
    args.output.mkdir(parents=True, exist_ok=False)
    run_id = uuid.uuid4().hex
    name = "pipeline-bench-" + run_id[:12]
    cm = name + "-scripts"
    created = []
    collected = False
    started = time.time()
    status = {"run_id": run_id, "complete": False, "started": started, "errors": []}
    try:
        context = command("kubectl", "config", "current-context").strip()
        if context != "kind-inference-lab":
            raise ValueError("Runner requires kind-inference-lab")
        active = get("pods", "-l", "app=pipeline-benchmark")["items"]
        if active:
            raise ValueError("Another benchmark Pod exists; collect/remove it before starting a run")
        initial = snapshot()
        write_json(args.output / "initial-state.json", initial)
        worker = worker_config(initial, config)
        policy = gateway_policy(initial, config["gateway_profile"])
        source = (HERE / "client.py").read_text()
        revision = command("git", "rev-parse", "HEAD").strip()
        dirty = bool(command("git", "status", "--porcelain").strip())
        manifest = pod_manifest(name, cm, run_id, worker, config)
        write_json(args.output / "manifest.json", {"run_id": run_id, "started": started, "config": config,
                   "gateway_policy": policy, "worker": worker, "context": context, "git_revision": revision,
                   "git_dirty": dirty, "client_sha256": hashlib.sha256(source.encode()).hexdigest()})
        write_json(args.output / "pod.json", manifest)
        (args.output / "client.py").write_text(source)
        write_json(args.output / "config.json", config)
        configmap = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": cm, "namespace": NAMESPACE},
                     "data": {"client.py": source, "config.json": json.dumps(config)}}
        kubectl("create", "-f", "-", input=json.dumps(configmap))
        created.append(("configmap", cm))
        kubectl("create", "-f", "-", input=json.dumps(manifest))
        created.append(("pod", name))
        deadline = time.monotonic() + 900 + sum(stage["duration_seconds"] for stage in config["stages"]) + config["timeout_seconds"] + 60
        with (args.output / "timeline.jsonl").open("x") as timeline:
            while time.monotonic() < deadline:
                current = snapshot()
                timeline.write(json.dumps({"time": time.time(), "state": current}) + "\n")
                timeline.flush()
                pod = next((pod for pod in current["pods"]["items"] if pod["metadata"]["name"] == name), None)
                if pod is None or pod.get("status", {}).get("phase") in ("Failed", "Succeeded"):
                    raise RuntimeError("Benchmark Pod disappeared or terminated before publishing status")
                result = subprocess.run(["kubectl", "-n", NAMESPACE, "exec", name, "--", "cat", "/results/status.json"],
                                        capture_output=True, text=True, timeout=20)
                if result.returncode == 0:
                    client_status = json.loads(result.stdout)
                    status["client"] = client_status
                    status["complete"] = client_status["complete"]
                    break
                time.sleep(5)
            else:
                raise TimeoutError("Benchmark exceeded startup and execution deadline")
    except (Exception, KeyboardInterrupt) as error:
        status["errors"].append(str(error) or type(error).__name__)
        if ("pod", name) in created:
            try:
                kubectl("exec", name, "--", "kill", "-INT", "1")
                time.sleep(17)
            except Exception as stop_error:
                status["errors"].append("Could not interrupt client: " + str(stop_error))
    finally:
        if ("pod", name) in created:
            try:
                (args.output / "client.log").write_text(kubectl("logs", name))
                kubectl("cp", f"{name}:/results", str(args.output / "client"))
                collected = True
            except Exception as error:
                status["errors"].append("Artifact collection failed; benchmark Pod retained: " + str(error))
        try:
            write_json(args.output / "final-state.json", snapshot())
        except Exception as error:
            status["errors"].append("Final snapshot: " + str(error))
        for kind, resource_name in reversed(created):
            if ("pod", name) in created and not collected:
                continue
            try:
                kubectl("delete", kind, resource_name, "--wait=false")
            except Exception as error:
                status["errors"].append("Cleanup: " + str(error))
        status["ended"] = time.time()
        status["complete"] = status["complete"] and not status["errors"]
        client_status = status.get("client", {})
        status["dashboard_url"] = dashboard_url(args.grafana_url, run_id, client_status.get("started", started),
                                                 client_status.get("ended", status["ended"]))
        write_json(args.output / "status.json", status)
        print(json.dumps(status, indent=2))
    return 0 if status["complete"] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=HERE / "workloads.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--grafana-url", default="http://127.0.0.1:3000")
    raise SystemExit(execute(parser.parse_args()))


if __name__ == "__main__":
    main()
