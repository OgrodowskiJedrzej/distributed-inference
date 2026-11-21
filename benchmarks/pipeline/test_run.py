import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import run


def state():
    config = json.loads((run.HERE / "workloads.json").read_text())
    worker = {"name": "worker", "args": [config["model"], "--revision", config["model_revision"],
              "--served-model-name", config["served_model"], "--max-model-len", "1024"],
              "volumeMounts": [{"name": "cache", "mountPath": "/cache"}]}
    deployment = {"metadata": {"name": "mock-vllm"}, "spec": {"template": {"spec": {
        "containers": [worker], "volumes": [{"name": "cache", "persistentVolumeClaim": {"claimName": "vllm-cache"}}]}}}}
    pod = {"metadata": {"name": "worker-1", "labels": {"app": "mock-vllm"}}, "spec": {"nodeName": "node"},
           "status": {"containerStatuses": [{"name": "worker", "ready": True,
                      "imageID": "docker-pullable://repo/worker@sha256:abc"}]}}
    return {"deployments": {"items": [deployment]}, "pods": {"items": [pod]}, "hpa": {"items": []}, "scaledobjects": {"items": []}}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((run.HERE / "workloads.json").read_text())

    def test_kubectl_forwards_manifest_input(self):
        with patch.object(run.subprocess, "run") as process:
            process.return_value = subprocess.CompletedProcess([], 0, "created", "")
            self.assertEqual(run.kubectl("create", "-f", "-", input="{}"), "created")
        process.assert_called_once_with(("kubectl", "-n", "inference", "create", "-f", "-"),
                                        check=True, capture_output=True, text=True, timeout=30, input="{}")

    def test_pod_targets_gateway_and_reuses_digest_read_only_cache(self):
        worker = run.worker_config(state(), self.config)
        manifest = run.pod_manifest("bench", "scripts", "id", worker, self.config)
        container = manifest["spec"]["containers"][0]
        self.assertEqual(container["image"], "repo/worker@sha256:abc")
        self.assertFalse(manifest["spec"]["automountServiceAccountToken"])
        self.assertEqual(container["ports"], [{"name": "metrics", "containerPort": 9091}])
        self.assertIn(self.config["tokenizer_revision"], container["args"][-1])
        self.assertTrue(next(mount for mount in container["volumeMounts"] if mount["name"] == "cache")["readOnly"])
        self.assertEqual(manifest["metadata"]["labels"]["app"], "pipeline-benchmark")

    def test_worker_mismatch_is_rejected(self):
        current = state()
        current["deployments"]["items"][0]["spec"]["template"]["spec"]["containers"][0]["args"][2] = "other"
        with self.assertRaisesRegex(ValueError, "--revision"):
            run.worker_config(current, self.config)

    def test_config_allows_idle_and_rejects_invalid_rates_and_bypass(self):
        run.validate(self.config)
        for value in (-1, float("nan"), float("inf")):
            self.config["stages"][0]["request_rate"] = value
            with self.assertRaises(ValueError):
                run.validate(self.config)
        self.config["stages"][0]["request_rate"] = 0
        self.config["endpoint"] = "http://vllm-router:30000/v1/chat/completions"
        with self.assertRaises(ValueError):
            run.validate(self.config)

    def test_existing_output_is_never_reused(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(run, "kubectl") as kube:
            args = argparse.Namespace(config=run.HERE / "workloads.json", output=Path(directory), grafana_url="http://grafana")
            with self.assertRaises(FileExistsError):
                run.execute(args)
            kube.assert_not_called()

    def test_dashboard_link_sets_run_and_millisecond_time(self):
        self.assertEqual(run.dashboard_url("http://grafana/", "run-id", 1.25, 2.5),
                         "http://grafana/d/inference-pipeline-experiment?var-run_id=run-id&from=1250&to=2500")

    def exercise(self, directory, copy_fails=False):
        calls = []
        initial = state()

        def command(*args, **kwargs):
            if args[:3] == ("kubectl", "config", "current-context"):
                return "kind-inference-lab\n"
            if args[:2] == ("git", "rev-parse"):
                return "revision"
            if args[:2] == ("git", "status"):
                return ""
            self.fail(str(args))

        def kubectl(*args, **kwargs):
            calls.append(args)
            if args[0] == "create":
                return "created"
            if args[0] in ("delete", "logs"):
                return ""
            if args[0] == "cp":
                if copy_fails:
                    raise RuntimeError("copy failed")
                return ""
            self.fail(str(args))

        def snapshot():
            value = state()
            if any(call[0] == "create" for call in calls):
                value["pods"]["items"].append({"metadata": {"name": "pipeline-bench-abcdefabcdef"}, "status": {"phase": "Running"}})
            return value

        args = argparse.Namespace(config=run.HERE / "workloads.json", output=Path(directory) / "new", grafana_url="http://grafana")
        with patch.object(run, "command", side_effect=command), patch.object(run, "kubectl", side_effect=kubectl), \
             patch.object(run, "get", return_value={"items": []}), patch.object(run, "snapshot", side_effect=snapshot), \
             patch.object(run, "gateway_policy", return_value={"requests_per_minute": 60}), \
             patch.object(run.uuid, "uuid4") as uuid, patch.object(run.subprocess, "run") as process:
            uuid.return_value.hex = "abcdefabcdef00000000000000000000"
            process.return_value = subprocess.CompletedProcess([], 0, json.dumps({"complete": True, "started": 10, "ended": 20}), "")
            with contextlib.redirect_stdout(io.StringIO()):
                result = run.execute(args)
        return result, calls, json.loads((args.output / "status.json").read_text())

    def test_success_only_creates_and_deletes_owned_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls, status = self.exercise(directory)
        self.assertEqual(result, 0)
        self.assertTrue(status["complete"])
        self.assertEqual([call[1] for call in calls if call[0] == "delete"], ["pod", "configmap"])
        self.assertTrue(all(call[0] in ("create", "delete", "logs", "cp") for call in calls))

    def test_collection_failure_retains_pod_and_configmap(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls, status = self.exercise(directory, copy_fails=True)
        self.assertEqual(result, 1)
        self.assertFalse(status["complete"])
        self.assertFalse(any(call[0] == "delete" for call in calls))
        self.assertIn("retained", status["errors"][0])


if __name__ == "__main__":
    unittest.main()
