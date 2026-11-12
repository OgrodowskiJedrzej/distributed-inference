import os
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()
WORKER_ID = os.getenv("HOSTNAME", "unknown")
logger = logging.getLogger("worker")
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())
logger.propagate = False


def log_event(event, request_id, **fields):
    logger.info(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": "INFO",
        "component": "worker",
        "event": event,
        "request_id": request_id,
        "pod": WORKER_ID,
        **fields,
    }, separators=(",", ":")))


@app.middleware("http")
async def log_request(request: Request, call_next):
    if request.url.path != "/v1/chat/completions":
        return await call_next(request)

    request_id = request.headers.get("x-request-id", uuid.uuid4().hex)
    started = time.monotonic()
    log_event("request_started", request_id, method=request.method, path=request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        log_event(
            "request_failed",
            request_id,
            method=request.method,
            path=request.url.path,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise
    response.headers["X-Request-ID"] = request_id
    log_event(
        "request_completed",
        request_id,
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return response


@app.get("/health")
def health():
    return {"status": "ok", "worker": WORKER_ID}


@app.get("/v1/models")
def models():
    return {
        "object": "list",
        "data": [
            {"id": "mock-qwen", "object": "model"}
        ],
    }


@app.post("/v1/chat/completions")
async def completion():
    await asyncio.sleep(0.2)

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "mock-qwen",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"Hello from {WORKER_ID}",
                },
                "finish_reason": "stop",
            }
        ],
    }
