"""One Settings class that reads every environment variable (gateway + worker)."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- infrastructure ---
    database_url: str = "postgresql+asyncpg://vn:vn@postgres:5432/vn"
    rabbitmq_url: str = "amqp://vn:vn@rabbitmq:5672/"
    redis_url: str = "redis://redis:6379/0"
    s3_endpoint: str = "http://s3:8333"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "vidnotes"

    # --- gateway ---
    gateway_base_url: str = "http://localhost:8000"
    admin_token: str = "change-me"
    youtube_api_key: str = ""
    metadata_timeout_sec: float = 3.0

    # --- Notion ---
    notion_client_id: str = ""
    notion_client_secret: str = ""
    notion_redirect_uri: str = ""
    notion_version: str = "2022-06-28"
    notion_rps: float = 2.5
    default_notion_parent_id: str = ""
    # Override only for tests / a local mock of the Notion API.
    notion_api_url: str = "https://api.notion.com/v1"
    # Default page sections (each can be overridden per job by the extension).
    notion_include_transcript: bool = True
    study_aids: bool = True

    # --- LLM ---
    llm_provider: str = "anthropic"  # anthropic | openai | ollama | gemini | fake
    llm_api_key: str = ""
    llm_model: str = "claude-haiku-5-5"
    llm_base_url: str = ""  # optional override for OpenAI-compatible endpoints
    llm_rpm: int = 10
    llm_tpm: int = 200_000
    llm_max_output_tokens: int = 16000
    llm_timeout_sec: float = 120.0
    llm_temperature: float = 0.2
    # Max LLM calls in flight across ALL workers (0 = unlimited). Set to 1-2 for a
    # local model so workers queue for it instead of timing out inside Ollama.
    llm_concurrency: int = 0
    # Ollama only: context window per request. Must hold system prompt + chunk + output.
    llm_num_ctx: int = 8192
    llm_keep_alive: str = "30m"

    # --- rate limiting ---
    youtube_rps: float = 2.0
    rl_max_wait_sec: float = 60.0

    # --- pipeline ---
    max_duration_sec: int = 1200
    max_frames: int = 8
    # Pad with evenly spaced frames when the LLM flags fewer than this (0 = off).
    min_frames: int = 0
    # Approximate length of the whole set of notes, split across transcript chunks.
    notes_words: int = 600
    # Transcript snippets are grouped into windows of this many seconds (timestamp granularity).
    segment_window_sec: float = 30.0
    chunk_tokens: int = 6000
    frame_parallelism: int = 3
    frame_timeout_sec: float = 30.0
    frame_best_of: int = 3
    frame_max_width: int = 1280
    frame_jpeg_quality: int = 80
    yt_proxy_url: str = ""

    # --- retries ---
    max_attempts: int = 5
    retry_delays: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [10, 30, 90, 180])
    lock_busy_delay_sec: int = 0  # 0 -> use the smallest retry delay

    # --- security ---
    token_enc_key: str = ""

    # --- demo / chaos ---
    fault_inject_429: str = ""  # e.g. "llm:0.3,notion:0.2"
    fault_inject_frame_fail: float = 0.0
    log_level: str = "INFO"

    @field_validator("retry_delays", mode="before")
    @classmethod
    def _parse_delays(cls, v):  # "10,30,90" -> [10, 30, 90]
        if isinstance(v, str):
            v = [int(x) for x in v.replace(" ", "").split(",") if x]
        if isinstance(v, int):
            v = [v]
        if not v:
            raise ValueError("RETRY_DELAYS must contain at least one delay")
        return sorted(int(x) for x in v)

    @field_validator("fault_inject_frame_fail", mode="before")
    @classmethod
    def _blank_float(cls, v):
        return 0.0 if v in ("", None) else v

    def fault_429_rates(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for part in self.fault_inject_429.split(","):
            if ":" in part:
                k, _, rate = part.partition(":")
                try:
                    out[k.strip().lower()] = float(rate)
                except ValueError:
                    pass
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
