"""Anthropic adapter (official SDK, structured JSON output)."""

from __future__ import annotations

from typing import Any

import anthropic

from vidnotes_common.config import Settings
from vidnotes_common.errors import ErrorCode, FatalError, RateLimited, RetryableError

from .base import LLMClient


def _retry_after(e: anthropic.APIStatusError) -> float | None:
    try:
        return float(e.response.headers.get("retry-after", ""))
    except (ValueError, AttributeError):
        return None


class AnthropicLLM(LLMClient):
    name = "anthropic"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._client: anthropic.AsyncAnthropic | None = None

    def _get_client(self) -> anthropic.AsyncAnthropic:
        # Built lazily so a missing key fails the job (LLM_AUTH_FAILED), not worker startup.
        if self._client is None:
            try:
                # Retries are owned by our queue (backoff + DLQ), not by the SDK.
                self._client = anthropic.AsyncAnthropic(
                    api_key=self.settings.llm_api_key or None,
                    max_retries=0,
                    timeout=self.settings.llm_timeout_sec,
                )
            except anthropic.AnthropicError as e:
                raise FatalError(ErrorCode.LLM_AUTH_FAILED, "LLM_API_KEY is not set on the server") from e
        return self._client

    async def complete_json(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> str:
        self.maybe_inject_429()
        client = self._get_client()
        try:
            resp = await client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.RateLimitError as e:
            raise RateLimited("llm", retry_after=_retry_after(e), message="Rate-limited by the LLM provider") from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise FatalError(ErrorCode.LLM_AUTH_FAILED) from e
        except anthropic.BadRequestError as e:
            raise FatalError(ErrorCode.LLM_ERROR, f"LLM rejected the request: {e.message}"[:300]) from e
        except anthropic.APIStatusError as e:  # 5xx, 529 overloaded, ...
            raise RetryableError(ErrorCode.LLM_ERROR, f"LLM provider error {e.status_code}") from e
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
            raise RetryableError(ErrorCode.LLM_ERROR, "LLM provider unreachable") from e
        except TypeError as e:  # SDK found no credentials (api key / token / profile)
            if "authentication" in str(e).lower():
                raise FatalError(ErrorCode.LLM_AUTH_FAILED, "LLM_API_KEY is not set on the server") from e
            raise

        if resp.stop_reason == "refusal":
            raise FatalError(ErrorCode.LLM_ERROR, "The model declined to summarize this video")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        if resp.stop_reason == "max_tokens":
            raise RetryableError(ErrorCode.LLM_BAD_OUTPUT, "LLM output was cut off (max_tokens)")
        return text

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
