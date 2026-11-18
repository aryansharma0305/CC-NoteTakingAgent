"""Gemini adapter (REST generateContent with JSON response mime type)."""

from __future__ import annotations

from typing import Any

import httpx

from vidnotes_common.config import Settings
from vidnotes_common.errors import ErrorCode, FatalError, RateLimited, RetryableError

from .base import LLMClient

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"


class GeminiLLM(LLMClient):
    name = "gemini"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._base = (settings.llm_base_url or GEMINI_API).rstrip("/")
        self._http = httpx.AsyncClient(timeout=settings.llm_timeout_sec)

    async def complete_json(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> str:
        self.maybe_inject_429()
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": max_tokens},
        }
        try:
            r = await self._http.post(
                f"{self._base}/models/{self.model}:generateContent",
                json=body,
                headers={"x-goog-api-key": self.settings.llm_api_key},
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise RetryableError(ErrorCode.LLM_ERROR, "LLM provider unreachable") from e
        if r.status_code == 429:
            raise RateLimited("llm", retry_after=None, message="Rate-limited by the LLM provider")
        if r.status_code in (401, 403):
            raise FatalError(ErrorCode.LLM_AUTH_FAILED)
        if r.status_code >= 500:
            raise RetryableError(ErrorCode.LLM_ERROR, f"LLM provider error {r.status_code}")
        if r.status_code >= 400:
            raise FatalError(ErrorCode.LLM_ERROR, f"LLM rejected the request: {r.text[:200]}")
        data = r.json()
        try:
            cand = data["candidates"][0]
            if cand.get("finishReason") == "MAX_TOKENS":
                raise RetryableError(ErrorCode.LLM_BAD_OUTPUT, "LLM output was cut off (max tokens)")
            return "".join(p.get("text", "") for p in cand["content"]["parts"])
        except (KeyError, IndexError) as e:
            raise RetryableError(ErrorCode.LLM_BAD_OUTPUT, "LLM returned no content") from e

    async def aclose(self) -> None:
        await self._http.aclose()
