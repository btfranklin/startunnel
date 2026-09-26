"""Logging helpers that add a request ID without request content."""

from __future__ import annotations

import contextvars
import logging
import re
from typing import Any
from uuid import UUID

from pythonjsonlogger.json import JsonFormatter

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
SENSITIVE_URL_PATTERNS = (
    re.compile(r"([?&](?:key|token|code|state)=)[^&\s]+", flags=re.IGNORECASE),
)


def redact_sensitive_urls(value: str) -> str:
    redacted = value
    for pattern in SENSITIVE_URL_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


def _redact_log_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_urls(value)
    if isinstance(value, dict):
        return {key: _redact_log_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_log_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_log_value(item) for item in value)
    return value


SAFE_LOG_MESSAGES = frozenset(
    {
        "Request completed.",
    }
)
SAFE_LOG_FIELDS = frozenset(
    {
        "asctime",
        "duration_ms",
        "levelname",
        "method",
        "name",
        "request_id",
        "route_name",
        "status",
    }
)


def current_request_id() -> UUID | None:
    """Return the active request ID for a transport-neutral audit event."""

    try:
        return UUID(request_id_var.get())
    except ValueError:
        return None


class RedactingJsonFormatter(JsonFormatter):
    """Emit only fixed events and bounded request metadata."""

    def process_log_record(self, log_data: dict[str, Any]) -> dict[str, Any]:
        message = str(log_data.get("message", ""))
        safe_message = message if message in SAFE_LOG_MESSAGES else "[REDACTED]"
        result = {
            key: _redact_log_value(value)
            for key, value in log_data.items()
            if key in SAFE_LOG_FIELDS
        }
        result["message"] = safe_message
        return result


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True
