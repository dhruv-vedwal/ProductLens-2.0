"""Structured logging copied in behaviour from legacy, without legacy coupling."""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any

import structlog

_CONFIGURED = False
_REDACT_KEYS = frozenset(
    {
        "authorization",
        "password",
        "api_key",
        "apikey",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "xi_api_key",
        "browserbase_api_key",
    }
)


def _redact_key(key: Any) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return normalized in _REDACT_KEYS or normalized.replace("_", "") in {"apikey", "secretkey"}


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if _redact_key(key) else redact_value(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


def configure_logging(*, force: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED and not force:
        return
    level = getattr(logging, (os.getenv("LOG_LEVEL") or "INFO").upper(), logging.INFO)
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if os.getenv("LOG_FORMAT") == "json"
        else structlog.dev.ConsoleRenderer()
    )
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level, force=force)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            lambda _, __, event: redact_value(event),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
    )
    _CONFIGURED = True


def get_logger(name: str = "productlens") -> Any:
    return structlog.get_logger(name)


def bind_run_context(**values: Any) -> None:
    structlog.contextvars.bind_contextvars(
        **{key: value for key, value in values.items() if value is not None}
    )


def clear_run_context() -> None:
    structlog.contextvars.clear_contextvars()


def safe_url(url: str) -> str:
    return re.sub(r"[?].*$", "", url)
