import asyncio
import importlib.util
import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import json
import random
import time
import unittest
from unittest.mock import patch

import client


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    def test_seeded_poisson_and_idle(self):
        first = list(client.arrivals(5, 10, random.Random(7)))
        self.assertEqual(first, list(client.arrivals(5, 10, random.Random(7))))
        self.assertNotEqual(first, list(client.arrivals(5, 10, random.Random(8))))
        self.assertTrue(all(0 < value < 10 for value in first))
        self.assertEqual(first, sorted(first))
        self.assertEqual(list(client.arrivals(0, 10, random.Random(7))), [])

    async def test_active_limit_does_not_stop_schedule_and_idle_drains(self):
        metrics = client.Metrics("test")
        records = []
        sent = []
        config = {"seed": 0, "timeout_seconds": 1,
                  "stages": [{"duration_seconds": 0.03, "request_rate": 1000},
                             {"duration_seconds": 0.07, "request_rate": 0}]}

        async def send(payload, index, stage):
            sent.append(index)
            await asyncio.sleep(0.05)

        with patch.object(client, "MAX_IN_FLIGHT", 1):
            await client.generate(config, metrics, send, lambda index: {}, records.append)
        self.assertEqual(len(sent), 1)
        self.assertGreater(metrics.values[("scheduled_total", ())], 1)
        self.assertGreater(metrics.values[("unsent_total", (("reason", "in_flight_limit"),))], 0)
        self.assertEqual(metrics.values[("target_requests_per_second", ())], 0)
        self.assertEqual([record["stage"] for record in records if record["event"] == "stage_started"], [0, 1])

    async def test_late_arrivals_are_not_sent_as_catchup(self):
        metrics = client.Metrics("test")
        records = []

        async def send(*args):
            self.fail("Late work must not be sent")

        def payload(index):
            time.sleep(0.005)
            return {}

        config = {"seed": 0, "timeout_seconds": 1, "stages": [{"duration_seconds": 0.02, "request_rate": 200}]}
        with patch.object(client, "MAX_SCHEDULER_LAG", 0.001):
            await client.generate(config, metrics, send, payload, records.append)
        self.assertGreater(metrics.values[("unsent_total", (("reason", "scheduler_lag"),))], 0)

    async def test_generator_failure_cancels_active_requests(self):
        finished = asyncio.Event()
        calls = 0

        async def send(*args):
            try:
                await asyncio.sleep(10)
            finally:
                finished.set()

        def payload(index):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("bad workload")
            return {}

        config = {"seed": 0, "timeout_seconds": 1, "stages": [{"duration_seconds": 0.1, "request_rate": 100}]}
        with self.assertRaises(ValueError):
            await client.generate(config, client.Metrics("test"), send, payload, lambda item: None)
        self.assertTrue(finished.is_set())

    async def test_drain_deadline_cancels_outstanding_requests(self):
        cancelled = []

        async def send(payload, index, stage):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(index)

        config = {"seed": 0, "timeout_seconds": 0.01,
                  "stages": [{"duration_seconds": 0.02, "request_rate": 1000}]}
        await client.generate(config, client.Metrics("test"), send, lambda index: {}, lambda item: None)
        self.assertGreater(len(cancelled), 0)
        self.assertEqual(len(cancelled), len(set(cancelled)))

    def test_histogram_encoding(self):
        metrics = client.Metrics('run"id')
        metrics.observe("ttft_seconds", 0.02)
        metrics.observe("ttft_seconds", 1.1)
        text = metrics.render()
        self.assertIn('# TYPE pipeline_client_ttft_seconds histogram', text)
        self.assertIn('le="+Inf",run_id="run\\"id"} 2.0', text)
        self.assertEqual(metrics.values[("ttft_seconds_count", ())], 2)
        self.assertAlmostEqual(metrics.values[("ttft_seconds_sum", ())], 1.12)
        self.assertIn("# TYPE pipeline_client_process_cpu_seconds_total counter", text)


@unittest.skipUnless(importlib.util.find_spec("aiohttp"), "aiohttp is tested inside the worker image")
class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiohttp
        from aiohttp import web

        self.aiohttp = aiohttp
        self.web = web
        self.attempts = 0
        self.mode = "success"
        self.invalid_event = None
        self.error_body = {"error": {"code": "requests", "message": "sensitive prompt"}}
        app = web.Application()
        app.router.add_post("/chat", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/chat"
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.15),
                                             connector=aiohttp.TCPConnector(limit=0))
        self.metrics = client.Metrics("test")
        self.records = []

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()

    async def handle(self, request):
        self.attempts += 1
        if self.mode in ("429", "500", "redirect"):
            if self.mode == "redirect":
                return self.web.Response(status=302, headers={"Location": "/chat"})
            return self.web.json_response(self.error_body, status=int(self.mode))
        response = self.web.StreamResponse(headers={"Content-Type": "text/event-stream", "X-Request-ID": "gateway-id"})
        await response.prepare(request)
        try:
            await response.write(b': comment\n\ndata: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
            await asyncio.sleep(0.02)
            await response.write(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
            if self.mode == "malformed":
                await response.write(("data: " + json.dumps(self.invalid_event) + "\n\ndata: [DONE]\n\n").encode())
                return response
            await asyncio.sleep(0.01)
            await response.write(b'data: {"choices":[{"delta":{"content":"world"}}]}\n\n')
            if self.mode == "timeout":
                await asyncio.sleep(0.2)
                return response
            if self.mode == "broken":
                request.transport.close()
                return response
            if self.mode == "no_done":
                return response
            if self.mode != "no_usage":
                await response.write(b'data: {"choices":[],"usage":{"completion_tokens":2}}\n\n')
            await response.write(b'data: [DONE]\n\n')
            return response
        except ConnectionResetError:
            return response

    async def perform(self):
        await client.request(self.session, self.url, {}, len(self.records), 0, self.metrics, self.records.append)
        return self.records[-1]

    async def test_success_uses_content_timing_usage_and_request_id(self):
        record = await self.perform()
        self.assertEqual(record["outcome"], "success")
        self.assertEqual(record["request_id"], "gateway-id")
        self.assertGreaterEqual(record["ttft_seconds"], 0.02)
        self.assertEqual(len(record["chunk_intervals_seconds"]), 1)
        self.assertGreater(record["e2e_seconds"], record["ttft_seconds"])
        self.assertEqual(self.metrics.values[("output_tokens_total", ())], 2)
        self.assertEqual(self.metrics.values[("ttft_seconds_count", ())], 1)
        self.assertEqual(self.attempts, 1)

    async def test_errors_do_not_retry_or_stop_following_requests(self):
        for mode, expected in (("429", "http_error"), ("500", "http_error"), ("redirect", "http_error"),
                               ("timeout", "timeout"), ("broken", "transport_error"), ("no_done", "stream_error")):
            with self.subTest(mode=mode):
                self.mode = mode
                record = await self.perform()
                self.assertEqual(record["outcome"], expected)
                self.assertNotIn("ttft_seconds", record)
        self.assertEqual(self.attempts, 6)
        self.assertNotIn(("ttft_seconds_count", ()), self.metrics.values)
        self.assertNotIn("sensitive prompt", json.dumps(self.records))
        self.mode = "success"
        self.assertEqual((await self.perform())["outcome"], "success")
        self.assertEqual(self.metrics.values[("in_flight", ())], 0)

    async def test_malformed_stream_shapes_fail_one_request_and_continue(self):
        events = [None, [], "text", 1, {"usage": []}, {"usage": "bad"},
                  {"choices": {}}, {"choices": [None]}, {"choices": [{"delta": None}]},
                  {"choices": [{"delta": {"content": 5}}]}]
        self.mode = "malformed"
        for event in events:
            with self.subTest(event=event):
                self.invalid_event = event
                record = await self.perform()
                self.assertEqual(record["outcome"], "stream_error")
                self.assertNotIn("ttft_seconds", record)
        self.assertEqual(self.attempts, len(events))
        self.assertNotIn(("ttft_seconds_count", ()), self.metrics.values)
        self.assertNotIn(("output_tokens_total", ()), self.metrics.values)
        self.assertEqual(self.metrics.values[("in_flight", ())], 0)
        self.mode = "success"
        self.assertEqual((await self.perform())["outcome"], "success")
        self.assertEqual(self.metrics.values[("ttft_seconds_count", ())], 1)

    async def test_malformed_http_error_body_keeps_http_error_outcome(self):
        self.mode = "429"
        for body in (None, [], {"error": None}, {"error": []}):
            with self.subTest(body=body):
                self.error_body = body
                record = await self.perform()
                self.assertEqual(record["outcome"], "http_error")
                self.assertEqual(record["http_status"], 429)
                self.assertEqual(record["reason"], "other")
        self.assertEqual(self.attempts, 4)
        self.assertNotIn(("ttft_seconds_count", ()), self.metrics.values)

    async def test_missing_usage_is_not_reported_as_zero_tokens(self):
        self.mode = "no_usage"
        record = await self.perform()
        self.assertEqual(record["outcome"], "success")
        self.assertIsNone(record["output_tokens"])
        self.assertNotIn(("output_tokens_total", ()), self.metrics.values)
        self.assertEqual(self.metrics.values[("missing_usage_total", ())], 1)

    async def test_cancelled_request_has_one_outcome_and_no_latency_sample(self):
        self.mode = "timeout"
        task = asyncio.create_task(self.perform())
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.records[0]["outcome"], "cancelled")
        self.assertNotIn(("ttft_seconds_count", ()), self.metrics.values)
        self.assertEqual(self.metrics.values[("in_flight", ())], 0)


@unittest.skipUnless(importlib.util.find_spec("aiohttp"), "aiohttp is tested inside the worker image")
class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, failing):
        config = json.loads((Path(client.__file__).parent / "workloads.json").read_text())
        config["stages"] = [{"duration_seconds": 0.01, "request_rate": 0}]
        loader = unittest.mock.Mock()
        if failing:
            loader.from_pretrained.side_effect = ValueError("tokenizer unavailable")
        else:
            loader.from_pretrained.return_value = object()
        pauses = []
        original_sleep = asyncio.sleep

        async def sleep(seconds):
            if seconds == 15:
                pauses.append(seconds)
                return
            await original_sleep(seconds)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.dict("sys.modules", {"transformers": SimpleNamespace(AutoTokenizer=loader)}), \
                 patch.object(client.importlib.metadata, "version", return_value="test-version"), \
                 patch.object(client.asyncio, "sleep", side_effect=sleep), contextlib.redirect_stdout(io.StringIO()):
                result = await client.run(config, "test", output, "/pinned-tokenizer", 0)
            saved = json.loads((output / "status.json").read_text())
            metrics = (output / "metrics.prom").read_text()
            records = [json.loads(line) for line in (output / "completed.jsonl").read_text().splitlines()]
            self.assertEqual((output / "workload.jsonl").read_text(), "")
            self.assertTrue((output / "versions.json").exists())
        self.assertEqual(pauses, [15])
        self.assertEqual(saved, result)
        loader.from_pretrained.assert_called_once_with("/pinned-tokenizer", local_files_only=True)
        return result, metrics, records

    async def test_idle_run_never_warms_up_and_publishes_complete_status(self):
        status, metrics, records = await self.exercise(False)
        self.assertTrue(status["complete"])
        self.assertIn('pipeline_client_phase{run_id="test"} 3', metrics)
        self.assertFalse(any(record["event"] == "request_completed" for record in records))

    async def test_startup_failure_publishes_incomplete_status_and_metrics(self):
        status, metrics, records = await self.exercise(True)
        self.assertFalse(status["complete"])
        self.assertEqual(status["error"], "ValueError")
        self.assertIn('pipeline_client_phase{run_id="test"} 4', metrics)
        self.assertTrue(any(record["event"] == "generator_failed" for record in records))


if __name__ == "__main__":
    unittest.main()
