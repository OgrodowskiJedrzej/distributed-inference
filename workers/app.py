import os
import asyncio
import time
import uuid

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()
WORKER_ID = os.getenv("HOSTNAME", "unknown")


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
