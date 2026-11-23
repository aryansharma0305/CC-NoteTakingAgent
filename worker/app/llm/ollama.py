"""Native Ollama adapter (POST /api/chat), the recommended way to run a local model.

Why not Ollama's OpenAI-compatible endpoint:
* `format: <JSON Schema>` turns on grammar-constrained decoding, so even a 3B
  model can only emit JSON of the right shape (no repair round-trips);
* `options.num_ctx` can be set per request; the /v1 endpoint silently uses the
  server default and truncates long prompts from the front (losing the rules);
* the response carries token counts and timings, which go into the debug trace.
"""

from __future__ import annotations

from typing import Any

import httpx

from vidnotes_common.config import Settings
from vidnotes_common.errors import ErrorCode, FatalError, RateLimited, RetryableError

from .base import LLMClient

DEFAULT_BASE = "http://host.docker.internal:11434"


def ollama_base(url: str) -> str:
    base = (url or DEFAULT_BASE).rstrip("/")
    # People often reuse the OpenAI-compatible URL (.../v1); the native API lives at the root.
    return base[:-3] if base.endswith("/v1") else base


class OllamaLLM(LLMClient):
    name = "ollama"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._base = ollama_base(settings.llm_base_url)
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(settings.llm_timeout_sec, connect=10.0))

    async def complete_json(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> str:
        self.maybe_inject_429()
        s = self.settings
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": schema,
            "keep_alive": s.llm_keep_alive,
            "options": {
                "temperature": s.llm_temperature,
                "top_p": 0.9,
                "repeat_penalty": 1.05,
                "num_ctx": s.llm_num_ctx,
                "num_predict": max_tokens,
            },
        }
        try:
            r = await self._http.post(f"{self._base}/api/chat", json=body)
        except httpx.TimeoutException as e:
            raise RetryableError(ErrorCode.LLM_ERROR,
                                 f"The local model took longer than {int(s.llm_timeout_sec)} s "
                                 "(raise LLM_TIMEOUT_SEC or use a smaller model)") from e
        except httpx.TransportError as e:
            raise RetryableError(ErrorCode.LLM_ERROR, f"Local model server (Ollama) unreachable at {self._base}") from e
        if r.status_code == 429:
            raise RateLimited("llm", retry_after=None, message="The local model server is busy")
        if r.status_code == 404:
            raise FatalError(ErrorCode.LLM_ERROR,
                             f"Model {self.model!r} is not installed in Ollama (run: ollama pull {self.model})")
        if r.status_code >= 500:
            raise RetryableError(ErrorCode.LLM_ERROR, f"Ollama error {r.status_code}: {r.text[:200]}")
        if r.status_code >= 400:
            raise FatalError(ErrorCode.LLM_ERROR, f"Ollama rejected the request: {r.text[:200]}")
        data = r.json()
        self.last_stats = _stats(data)
        if data.get("done_reason") == "length":
            raise RetryableError(ErrorCode.LLM_BAD_OUTPUT,
                                 "The local model's output was cut off (raise LLM_MAX_OUTPUT_TOKENS / LLM_NUM_CTX)")
        return (data.get("message") or {}).get("content") or ""

    async def aclose(self) -> None:
        await self._http.aclose()


def _stats(d: dict[str, Any]) -> dict[str, Any]:
    ns = 1e9
    out: dict[str, Any] = {
        "prompt_tokens": d.get("prompt_eval_count"),
        "output_tokens": d.get("eval_count"),
        "load_sec": round((d.get("load_duration") or 0) / ns, 2),
        "prompt_sec": round((d.get("prompt_eval_duration") or 0) / ns, 2),
        "gen_sec": round((d.get("eval_duration") or 0) / ns, 2),
    }
    if d.get("eval_count") and d.get("eval_duration"):
        out["tokens_per_sec"] = round(d["eval_count"] / (d["eval_duration"] / ns), 1)
    return out
