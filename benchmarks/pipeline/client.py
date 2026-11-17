import argparse
import asyncio
from collections import defaultdict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import math
from pathlib import Path
import random
import resource
import signal
import sys
import threading
import time


VERSION = "pipeline-stream-v1"
BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)
MAX_IN_FLIGHT = 256
MAX_SCHEDULER_LAG = 0.1


def validate(config):
    for key in ("model", "model_revision", "served_model", "tokenizer_revision", "endpoint"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError(f"Missing {key}")
    if not config["endpoint"].startswith("http://inference-gateway:8000/"):
        raise ValueError("This runner only targets the in-cluster inference-gateway")
    for key in ("input_tokens", "output_tokens", "seed"):
        if type(config.get(key)) is not int:
            raise ValueError(f"{key} must be an integer")
    if config["input_tokens"] < 64 or config["output_tokens"] < 1:
        raise ValueError("Require input_tokens >= 64 and output_tokens >= 1")
    if not math.isfinite(config["timeout_seconds"]) or config["timeout_seconds"] <= 0:
        raise ValueError("timeout_seconds must be positive and finite")
    if not config.get("stages"):
        raise ValueError("At least one stage is required")
    for stage in config["stages"]:
        if not math.isfinite(stage["duration_seconds"]) or stage["duration_seconds"] <= 0:
            raise ValueError("Stage duration must be positive and finite")
        if not math.isfinite(stage["request_rate"]) or stage["request_rate"] < 0:
            raise ValueError("Stage request_rate must be nonnegative and finite")


def arrivals(rate, duration, rng):
    elapsed = 0.0
    while rate > 0:
        elapsed += rng.expovariate(rate)
        if elapsed >= duration:
            break
        yield elapsed


def prompts(tokenizer, input_tokens, seed):
    prefix = " shared" * 64
    prefix_ids = tokenizer.encode(prefix, add_special_tokens=False)
    if len(prefix_ids) != 64 or len(tokenizer.encode(" shared", add_special_tokens=False)) != 1:
        raise ValueError("Tokenizer must preserve the common 64-token prefix")
    candidates = ("apple", "river", "stone", "house", "green", "paper", "water", "music",
                  "light", "cloud", "table", "bread", "plant", "ocean", "book", "tree")
    words = [" " + word for word in candidates
             if len(tokenizer.encode(" " + word, add_special_tokens=False)) == 1]
    if len(words) < 2:
        raise ValueError("Tokenizer has too few single-token benchmark words")
    rng = random.Random(seed)
    while True:
        text = prefix + "".join(rng.choice(words) for _ in range(input_tokens - 64))
        ids = tokenizer.encode(text, add_special_tokens=False)
        if len(ids) != input_tokens or ids[:64] != prefix_ids:
            raise ValueError("Synthetic prompt does not preserve token lengths and prefix")
        yield text


class Metrics:
    def __init__(self, run_id):
        self.run_id = run_id
        self.values = defaultdict(float)
        self.types = {}
        self.lock = threading.Lock()

    def add(self, name, value=1, kind="counter", **labels):
        with self.lock:
            self.types[name] = kind
            self.values[(name, tuple(sorted(labels.items())))] += value

    def set(self, name, value, kind="gauge", **labels):
        with self.lock:
            self.types[name] = kind
            self.values[(name, tuple(sorted(labels.items())))] = value

    def observe(self, name, value):
        with self.lock:
            self.types[name] = "histogram"
            self.values[(name + "_sum", ())] += value
            self.values[(name + "_count", ())] += 1
            for bound in (*BUCKETS, math.inf):
                if value <= bound:
                    label = "+Inf" if math.isinf(bound) else str(bound)
                    self.values[(name + "_bucket", (("le", label),))] += 1
                else:
                    self.values[(name + "_bucket", (("le", str(bound)),))] += 0

    def render(self):
        usage = resource.getrusage(resource.RUSAGE_SELF)
        self.set("process_cpu_seconds_total", usage.ru_utime + usage.ru_stime, kind="counter")
        try:
            import os
            resident = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except FileNotFoundError:
            resident = usage.ru_maxrss if sys.platform == "darwin" else usage.ru_maxrss * 1024
        self.set("process_resident_memory_bytes", resident)
        with self.lock:
            lines = [f"# TYPE pipeline_client_{name} {kind}" for name, kind in sorted(self.types.items())]
            for (name, labels), value in sorted(self.values.items()):
                fields = {"run_id": self.run_id, **dict(labels)}
                label_text = ",".join(f"{key}={json.dumps(str(item))}" for key, item in sorted(fields.items()))
                lines.append(f"pipeline_client_{name}{{{label_text}}} {value}")
        return "\n".join(lines) + "\n"


def serve_metrics(metrics, port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            body = metrics.render().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def sse_events(content):
    data = []
    async for line in content:
        text = line.decode("utf-8").rstrip("\r\n")
        if not text:
            if data:
                yield "\n".join(data)
                data = []
        elif text.startswith("data:"):
            data.append(text[5:].removeprefix(" "))
    if data:
        yield "\n".join(data)


async def request(session, endpoint, payload, index, stage, metrics, emit):
    import aiohttp

    started = time.monotonic()
    record = {"event": "request_completed", "index": index, "stage": stage,
              "wall_started": time.time(), "http_status": 0, "outcome": "stream_error", "reason": "none"}
    metrics.add("sent_total")
    metrics.add("in_flight", kind="gauge")
    first = last = None
    intervals = []
    output_tokens = None
    try:
        async with session.post(endpoint, json=payload, allow_redirects=False) as response:
            record["http_status"] = response.status
            record["request_id"] = response.headers.get("X-Request-ID")
            if response.status != 200:
                record["outcome"] = "http_error"
                try:
                    body = await response.json()
                    error = body.get("error") if isinstance(body, dict) else None
                    code = error.get("code") if isinstance(error, dict) else None
                    record["reason"] = code if code in ("requests", "tokens", "concurrency", "rate_limiter_unavailable") else "other"
                except (ValueError, aiohttp.ContentTypeError):
                    record["reason"] = "other"
                return
            done = False
            async for event in sse_events(response.content):
                now = time.monotonic()
                if event == "[DONE]":
                    done = True
                    break
                data = json.loads(event)
                if not isinstance(data, dict) or data.get("error"):
                    return
                usage = data.get("usage")
                if usage is not None and not isinstance(usage, dict):
                    return
                usage = usage or {}
                if type(usage.get("completion_tokens")) is int and usage["completion_tokens"] >= 0:
                    output_tokens = usage["completion_tokens"]
                choices = data.get("choices", [])
                if not isinstance(choices, list):
                    return
                content = None
                if choices:
                    if not isinstance(choices[0], dict):
                        return
                    delta = choices[0].get("delta", {})
                    if not isinstance(delta, dict):
                        return
                    content = delta.get("content")
                    if content is not None and not isinstance(content, str):
                        return
                if content:
                    if first is None:
                        first = now
                    if last is not None:
                        intervals.append(now - last)
                    last = now
            if not done or first is None:
                return
            record.update(outcome="success", ttft_seconds=first - started, e2e_seconds=last - started,
                          protocol_tail_seconds=time.monotonic() - last, chunk_intervals_seconds=intervals,
                          output_tokens=output_tokens)
            for name in ("ttft_seconds", "e2e_seconds", "protocol_tail_seconds"):
                metrics.observe(name, record[name])
            for interval in intervals:
                metrics.observe("chunk_interval_seconds", interval)
            if output_tokens is not None:
                metrics.add("output_tokens_total", output_tokens)
                metrics.add("usage_requests_total")
            else:
                metrics.add("missing_usage_total")
    except asyncio.TimeoutError:
        record["outcome"] = "timeout"
    except asyncio.CancelledError:
        record["outcome"] = "cancelled"
        raise
    except aiohttp.ClientError:
        record["outcome"] = "transport_error"
    except (ValueError, KeyError, TypeError, IndexError, UnicodeError):
        record["outcome"] = "stream_error"
    finally:
        metrics.add("in_flight", -1, kind="gauge")
        metrics.add("completed_total", outcome=record["outcome"], http_status=str(record["http_status"]), reason=record["reason"])
        record.update(wall_finished=time.time(), duration_seconds=time.monotonic() - started)
        emit(record)


async def generate(config, metrics, send, make_payload, emit):
    rng = random.Random(config["seed"])
    tasks = set()
    start = time.monotonic()
    stage_start = start
    index = 0
    try:
        for stage_index, stage in enumerate(config["stages"]):
            metrics.set("stage", stage_index)
            metrics.set("target_requests_per_second", stage["request_rate"])
            emit({"event": "stage_started", "stage": stage_index, **stage})
            for offset in arrivals(stage["request_rate"], stage["duration_seconds"], rng):
                due = stage_start + offset
                await asyncio.sleep(max(0, due - time.monotonic()))
                finished = {task for task in tasks if task.done()}
                for task in finished:
                    task.result()
                tasks.difference_update(finished)
                payload = make_payload(index)
                lag = max(0, time.monotonic() - due)
                metrics.add("scheduled_total")
                metrics.observe("scheduler_lag_seconds", lag)
                if lag > MAX_SCHEDULER_LAG or len(tasks) >= MAX_IN_FLIGHT:
                    reason = "scheduler_lag" if lag > MAX_SCHEDULER_LAG else "in_flight_limit"
                    metrics.add("unsent_total", reason=reason)
                    emit({"event": "request_unsent", "index": index, "stage": stage_index, "reason": reason})
                else:
                    task = asyncio.create_task(send(payload, index, stage_index))
                    tasks.add(task)
                index += 1
                await asyncio.sleep(0)
                finished = {task for task in tasks if task.done()}
                for task in finished:
                    task.result()
                tasks.difference_update(finished)
            stage_start += stage["duration_seconds"]
            await asyncio.sleep(max(0, stage_start - time.monotonic()))
        metrics.set("target_requests_per_second", 0)
        metrics.set("phase", 2)
        emit({"event": "draining"})
        if tasks:
            finished, pending = await asyncio.wait(tasks, timeout=config["timeout_seconds"])
            for task in finished:
                task.result()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        metrics.set("target_requests_per_second", 0)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run(config, run_id, output, tokenizer_path, port):
    import aiohttp
    from transformers import AutoTokenizer

    validate(config)
    metrics = Metrics(run_id)
    for name in ("scheduled_total", "sent_total", "output_tokens_total", "usage_requests_total", "missing_usage_total"):
        metrics.add(name, 0)
    metrics.set("in_flight", 0)
    metrics.set("phase", 0)
    metrics.set("info", 1, version=VERSION, seed=str(config["seed"]), profile=config["gateway_profile"],
                workload=f"{config['input_tokens']}-{config['output_tokens']}", model=config["model"],
                model_revision=config["model_revision"], tokenizer_revision=config["tokenizer_revision"])
    server = serve_metrics(metrics, port)
    started = time.time()
    status = {"complete": False, "started": started, "version": VERSION, "run_id": run_id}
    with (output / "completed.jsonl").open("x") as records, (output / "workload.jsonl").open("x") as workload:
        def emit(record):
            record = {"run_id": run_id, "time": time.time(), **record}
            text = json.dumps(record, allow_nan=False)
            records.write(text + "\n")
            records.flush()
            print(text, flush=True)

        try:
            versions = {name: importlib.metadata.version(name) for name in ("aiohttp", "transformers", "vllm")}
            (output / "versions.json").write_text(json.dumps({"client": VERSION, "python": sys.version, **versions}, indent=2))
            tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, local_files_only=True)
            texts = prompts(tokenizer, config["input_tokens"], config["seed"])

            def payload(index):
                text = next(texts)
                messages = [{"role": "user", "content": text}]
                count = len(tokenizer.apply_chat_template(messages, add_generation_prompt=True))
                if count + config["output_tokens"] > config["max_model_len"]:
                    raise ValueError("Chat template plus output exceeds max_model_len")
                workload.write(json.dumps({"index": index, "prompt": text, "prompt_tokens": count}) + "\n")
                return {"model": config["served_model"], "messages": messages, "stream": True,
                        "stream_options": {"include_usage": True}, "temperature": 0,
                        "max_tokens": config["output_tokens"], "min_tokens": config["output_tokens"], "ignore_eos": True}

            metrics.set("phase", 1)
            timeout = aiohttp.ClientTimeout(total=config["timeout_seconds"])
            async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(limit=0),
                                             trust_env=False) as session:
                async def send(body, index, stage):
                    await request(session, config["endpoint"], body, index, stage, metrics, emit)

                await generate(config, metrics, send, payload, emit)
            status["complete"] = True
        except asyncio.CancelledError:
            status["error"] = "interrupted"
        except Exception as error:
            status["error"] = type(error).__name__
            emit({"event": "generator_failed", "error_type": type(error).__name__})
        finally:
            workload.flush()
            status.update(ended=time.time(), workload_sha256=hashlib.sha256((output / "workload.jsonl").read_bytes()).hexdigest())
            metrics.set("phase", 3 if status["complete"] else 4)
            emit({"event": "run_finished", **status})
            (output / "metrics.prom").write_text(metrics.render())
            await asyncio.sleep(15)
            (output / "status.json").write_text(json.dumps(status, indent=2))
            await asyncio.to_thread(server.shutdown)
            server.server_close()
    return status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--tokenizer", required=True)
    parser.add_argument("--output", type=Path, default=Path("/results"))
    parser.add_argument("--metrics-port", type=int, default=9091)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    args.output.mkdir(exist_ok=True)

    async def execute():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        return await run(config, args.run_id, args.output, args.tokenizer, args.metrics_port)

    asyncio.run(execute())
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
