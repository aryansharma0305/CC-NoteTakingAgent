"""structlog JSON logging with job_id binding, shared by gateway and worker."""

from __future__ import annotations

import logging
import sys

try:
    import structlog
except ImportError:  # pragma: no cover - structlog is optional for the shared package
    structlog = None  # type: ignore[assignment]


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(stream=sys.stdout, level=level.upper(), format="%(message)s")
    for noisy in ("aio_pika", "aiormq", "httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if structlog is None:
        return
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "vidnotes"):
    if structlog is None:
        return logging.getLogger(name)
    return structlog.get_logger(name)
