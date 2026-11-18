"""Error codes, exception classes and user-facing copy.

Workers classify every failure into one of three exception types:

* RateLimited  -- an external API said 429 (or our own limiter gave up waiting).
                  Re-queued with backoff; the job shows as "waiting".
* RetryableError -- transient (5xx, timeouts, YouTube "unavailable").
                  Re-queued with backoff until MAX_ATTEMPTS, then DLQ.
* FatalError   -- will never succeed (video too long, no captions, Notion 401).
                  Job goes straight to FAILED.

The extension's lib/status-text.js mirrors USER_MESSAGES; keep them in sync.
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    # Submission (gateway, 4xx)
    INVALID_URL = "INVALID_URL"
    INVALID_REQUEST = "INVALID_REQUEST"
    MISSING_NOTION_TOKEN = "MISSING_NOTION_TOKEN"
    VIDEO_TOO_LONG = "VIDEO_TOO_LONG"
    LIVE_STREAM = "LIVE_STREAM"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_NOT_FINISHED = "JOB_NOT_FINISHED"
    UNAUTHORIZED = "UNAUTHORIZED"
    OAUTH_EXCHANGE_FAILED = "OAUTH_EXCHANGE_FAILED"
    OAUTH_NOT_CONFIGURED = "OAUTH_NOT_CONFIGURED"
    NOT_IN_DLQ = "NOT_IN_DLQ"

    # Pipeline
    VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
    NO_TRANSCRIPT = "NO_TRANSCRIPT"
    YOUTUBE_BLOCKED = "YOUTUBE_BLOCKED"
    RATE_LIMITED = "RATE_LIMITED"
    LLM_ERROR = "LLM_ERROR"
    LLM_AUTH_FAILED = "LLM_AUTH_FAILED"
    LLM_BAD_OUTPUT = "LLM_BAD_OUTPUT"
    STREAM_UNAVAILABLE = "STREAM_UNAVAILABLE"
    FRAME_EXTRACTION_FAILED = "FRAME_EXTRACTION_FAILED"
    FRAME_UPLOAD_FAILED = "FRAME_UPLOAD_FAILED"
    FRAME_MISSING_CAPTION = "FRAME_MISSING_CAPTION"
    NOTION_UNAUTHORIZED = "NOTION_UNAUTHORIZED"
    NOTION_FORBIDDEN = "NOTION_FORBIDDEN"
    NOTION_PARENT_NOT_FOUND = "NOTION_PARENT_NOT_FOUND"
    NOTION_BAD_REQUEST = "NOTION_BAD_REQUEST"
    NOTION_ERROR = "NOTION_ERROR"
    NO_NOTION_DESTINATION = "NO_NOTION_DESTINATION"
    RETRIES_EXHAUSTED = "RETRIES_EXHAUSTED"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


USER_MESSAGES: dict[str, str] = {
    ErrorCode.INVALID_URL: "That doesn't look like a YouTube video link.",
    ErrorCode.INVALID_REQUEST: "The request was malformed.",
    ErrorCode.MISSING_NOTION_TOKEN: "Connect Notion first, then try again.",
    ErrorCode.VIDEO_TOO_LONG: "This video is longer than the 20-minute limit.",
    ErrorCode.LIVE_STREAM: "Live streams and premieres can't be summarized.",
    ErrorCode.BROKER_UNAVAILABLE: "The service is temporarily unavailable. Try again in a moment.",
    ErrorCode.JOB_NOT_FOUND: "This job no longer exists.",
    ErrorCode.JOB_NOT_FINISHED: "The job hasn't finished yet.",
    ErrorCode.UNAUTHORIZED: "Not authorized.",
    ErrorCode.OAUTH_EXCHANGE_FAILED: "Couldn't finish connecting to Notion. Please try again.",
    ErrorCode.OAUTH_NOT_CONFIGURED: "Notion sign-in isn't configured on the server.",
    ErrorCode.NOT_IN_DLQ: "That job isn't in the dead-letter queue.",
    ErrorCode.VIDEO_UNAVAILABLE: "YouTube says this video is unavailable (private, removed or region-locked).",
    ErrorCode.NO_TRANSCRIPT: "This video has no captions, so it can't be summarized.",
    ErrorCode.YOUTUBE_BLOCKED: "YouTube is blocking requests from the server right now.",
    ErrorCode.RATE_LIMITED: "Rate-limited by an external service.",
    ErrorCode.LLM_ERROR: "The summarizer had a problem.",
    ErrorCode.LLM_AUTH_FAILED: "The server's AI key was rejected.",
    ErrorCode.LLM_BAD_OUTPUT: "The summarizer returned something unreadable.",
    ErrorCode.STREAM_UNAVAILABLE: "Couldn't open the video stream to capture frames.",
    ErrorCode.FRAME_EXTRACTION_FAILED: "A frame couldn't be captured.",
    ErrorCode.FRAME_UPLOAD_FAILED: "A frame couldn't be uploaded to Notion.",
    ErrorCode.FRAME_MISSING_CAPTION: "A frame was dropped because it had no caption.",
    ErrorCode.NOTION_UNAUTHORIZED: "Notion access was revoked. Reconnect Notion.",
    ErrorCode.NOTION_FORBIDDEN: "The integration can't write to that Notion page. Share it with the integration.",
    ErrorCode.NOTION_PARENT_NOT_FOUND: "The Notion destination page wasn't found or isn't shared with the integration.",
    ErrorCode.NOTION_BAD_REQUEST: "Notion rejected the page.",
    ErrorCode.NOTION_ERROR: "Notion had a problem.",
    ErrorCode.NO_NOTION_DESTINATION: "No Notion page is shared with the integration to publish into.",
    ErrorCode.RETRIES_EXHAUSTED: "Gave up after several attempts.",
    ErrorCode.UNEXPECTED_ERROR: "Something went wrong.",
}


def user_message(code: str) -> str:
    return USER_MESSAGES.get(code, f"Something went wrong (code {code}).")


class PipelineError(Exception):
    code: str = ErrorCode.UNEXPECTED_ERROR

    def __init__(self, code: str | None = None, message: str | None = None, **details: Any):
        self.code = code or self.code
        self.message = message or user_message(self.code)
        self.details = details
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            d["details"] = self.details
        return d


class RetryableError(PipelineError):
    """Transient failure: retry with backoff, then dead-letter."""


class RateLimited(RetryableError):
    """An external API (or our limiter) told us to slow down."""

    code = ErrorCode.RATE_LIMITED

    def __init__(self, service: str, retry_after: float | None = None, message: str | None = None):
        self.service = service
        self.retry_after = retry_after
        super().__init__(
            ErrorCode.RATE_LIMITED,
            message or f"Rate-limited by {service}",
            service=service,
            retry_after=retry_after,
        )


class FatalError(PipelineError):
    """Will never succeed: mark FAILED immediately."""


def api_error(code: str, message: str | None = None, **details: Any) -> dict[str, Any]:
    """Gateway error body: {"error": {"code", "message"}}."""
    body: dict[str, Any] = {"code": code, "message": message or user_message(code)}
    if details:
        body["details"] = details
    return {"error": body}
