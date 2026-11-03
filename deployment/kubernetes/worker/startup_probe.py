import json
import os
import urllib.request


BASE_URL = "http://127.0.0.1:8000"
REQUEST_TIMEOUT_SECONDS = float(os.getenv("STARTUP_PROBE_TIMEOUT_SECONDS", "4"))


with urllib.request.urlopen(f"{BASE_URL}/v1/models", timeout=2) as response:
    model = json.load(response)["data"][0]["id"]

payload = json.dumps({
    "model": model,
    "messages": [{"role": "user", "content": "ping"}],
    "max_tokens": 1,
}).encode()
request = urllib.request.Request(
    f"{BASE_URL}/v1/chat/completions",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)

with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
    if response.status != 200:
        raise SystemExit(1)
