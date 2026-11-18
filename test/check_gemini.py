"""Check that a Gemini API key works.

Usage:
    python3 test/check_gemini.py                # reads GEMINI_API_KEY from test/.env
    GEMINI_API_KEY=your-key python3 test/check_gemini.py
    python3 test/check_gemini.py                # prompts for the key
    python3 test/check_gemini.py --model gemini-2.5-flash

Step 1 lists the models the key can use (free, no tokens).
Step 2 sends a tiny prompt to one model and prints the reply.
Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://generativelanguage.googleapis.com/v1beta"
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env(path: str = ENV_FILE) -> None:
    """Load KEY=VALUE lines from test/.env. Real environment variables win.
    Placeholders like <your-key> and blank values are ignored."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v and not v.startswith("<"):
                os.environ.setdefault(k.strip(), v)


def call(method: str, path: str, key: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}/{path}",
        data=data,
        method=method,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {}
    except urllib.error.URLError as e:
        sys.exit(f"FAIL  Could not reach Google: {e.reason}")


def version(model: str) -> tuple[float, ...]:
    """'gemini-3.8-flash' -> (3.8,) so newer models sort higher."""
    import re

    return tuple(float(n) for n in re.findall(r"\d+(?:\.\d+)?", model)) or (0.0,)


def explain(status: int, payload: dict) -> str:
    msg = payload.get("error", {}).get("message", "")
    hints = {
        400: "Key is malformed or invalid.",
        403: "Key rejected: wrong key, API not enabled, or restricted to other APIs/IPs.",
        404: "Model not found for this key. Try another --model.",
        429: "Key works, but you hit a rate limit or quota.",
    }
    return f"HTTP {status}: {hints.get(status, 'Unexpected error.')} {msg}".strip()


def main() -> None:
    ap = argparse.ArgumentParser(description="Check a Gemini API key.")
    ap.add_argument("--model", help="model to test, e.g. gemini-2.5-flash (default: first flash model found)")
    args = ap.parse_args()
    load_env()
    args.model = args.model or os.environ.get("GEMINI_MODEL")

    key = os.environ.get("GEMINI_API_KEY") or getpass.getpass("Gemini API key: ")
    key = key.strip()
    if not key:
        sys.exit("FAIL  No key given.")

    # 1. List models (validates the key without spending tokens).
    status, payload = call("GET", "models?pageSize=200", key)
    if status != 200:
        sys.exit(f"FAIL  {explain(status, payload)}")
    models = [
        m["name"].removeprefix("models/")
        for m in payload.get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]
    print(f"OK    Key is valid. {len(models)} models support generateContent.")

    if args.model:
        candidates = [args.model]
    else:
        # Newest plain "flash" models first; listed models can still be closed to new keys.
        flash = [m for m in models if "flash" in m and not any(x in m for x in ("lite", "image", "tts", "live", "preview", "exp"))]
        candidates = sorted(flash, key=version, reverse=True)[:5] or models[:1]
    if not candidates:
        sys.exit("FAIL  No usable models for this key.")

    # 2. Tiny generation request.
    body = {
        "contents": [{"parts": [{"text": "Reply with exactly: Gemini key works"}]}],
        "generationConfig": {"maxOutputTokens": 50},
    }
    for model in candidates:
        status, payload = call("POST", f"models/{model}:generateContent", key, body)
        if status == 200:
            break
        print(f"SKIP  {model}: {explain(status, payload)[:160]}")
        if status not in (404, 500, 503) or args.model:
            sys.exit(f"FAIL  {model}: {explain(status, payload)}")
    else:
        sys.exit("FAIL  None of the tried models are available. Pass one with --model.")
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        text = "(no text returned)"
    print(f"OK    {model} replied: {text}")


if __name__ == "__main__":
    main()
