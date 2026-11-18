"""OpenAI-compatible chat-completions adapter (OpenAI, Groq, Together, vLLM, ...).
Set LLM_BASE_URL for non-OpenAI endpoints."""

from __future__ import annotations

from typing import Any

import httpx

from vidnotes_common.config import Settings
from vidnotes_common.errors import ErrorCode, FatalError, RateLimited, RetryableError

from .base import LLMClient


class OpenAICompatLLM(LLMClient):
    name = "openai"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._base = (settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")
        self._http = httpx.AsyncClient(timeout=settings.llm_timeout_sec)

    async def complete_json(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> str:
        self.maybe_inject_429()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": max_tokens,
        }
        try:
            r = await self._http.post(
                f"{self._base}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise RetryableError(ErrorCode.LLM_ERROR, "LLM provider unreachable") from e
        if r.status_code == 429:
            try:
                ra = float(r.headers.get("retry-after", ""))
            except ValueError:
                ra = None
            raise RateLimited("llm", retry_after=ra, message="Rate-limited by the LLM provider")
        if r.status_code in (401, 403):
            raise FatalError(ErrorCode.LLM_AUTH_FAILED)
        if r.status_code >= 500:
            raise RetryableError(ErrorCode.LLM_ERROR, f"LLM provider error {r.status_code}")
        if r.status_code >= 400:
            raise FatalError(ErrorCode.LLM_ERROR, f"LLM rejected the request: {r.text[:200]}")
        choice = r.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise RetryableError(ErrorCode.LLM_BAD_OUTPUT, "LLM output was cut off (length)")
        return choice["message"].get("content") or ""

    async def aclose(self) -> None:
        await self._http.aclose()
