"""Provider-agnostic LLM interface. Each adapter maps its SDK/HTTP errors onto
RateLimited / RetryableError / FatalError so the retry logic stays generic."""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import Any

from vidnotes_common.config import Settings
from vidnotes_common.errors import RateLimited


class LLMClient(ABC):
    name = "llm"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = settings.llm_model
        self._fault = settings.fault_429_rates().get("llm", 0.0)
        # Provider-reported usage/timing of the most recent call (for the debug trace).
        self.last_stats: dict[str, Any] | None = None

    def maybe_inject_429(self) -> None:
        if self._fault and random.random() < self._fault:
            raise RateLimited("llm", retry_after=None, message="Rate-limited by the LLM provider (injected fault)")

    @abstractmethod
    async def complete_json(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> str:
        """Return the model's raw JSON text."""

    async def aclose(self) -> None:  # pragma: no cover - optional
        return None


def make_llm(settings: Settings) -> LLMClient:
    provider = settings.llm_provider.lower()
    if provider == "anthropic":
        from .anthropic_client import AnthropicLLM

        return AnthropicLLM(settings)
    if provider in ("openai", "openai_compat", "openai-compatible"):
        from .openai_compat import OpenAICompatLLM

        return OpenAICompatLLM(settings)
    if provider in ("ollama", "local"):
        from .ollama import OllamaLLM

        return OllamaLLM(settings)
    if provider == "gemini":
        from .gemini import GeminiLLM

        return GeminiLLM(settings)
    if provider == "fake":
        from .fake import FakeLLM

        return FakeLLM(settings)
    raise ValueError(f"unknown LLM_PROVIDER {settings.llm_provider!r}")
