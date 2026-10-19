"""Emit operational logs without raw HTTP or SQL text."""

from __future__ import annotations

import contextvars
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
REQUEST_COMPLETED = "Request completed."
CYCLE_DELETION_FAILED = "Cycle content deletion failed."


def current_request_id() -> UUID | None:
    """Return the active request ID for a transport-neutral audit event."""

    try:
        return UUID(request_id_var.get())
    except ValueError:
        return None


class SafeJsonFormatter(logging.Formatter):
    """Write safe log fields without exception values or arbitrary extras."""

    def format(self, record: logging.LogRecord) -> str:
        logger = record.name
        raw_http = logger in {
            "django.request",
            "django.server",
            "django.security",
            "uvicorn.access",
        } or logger.startswith(("django.request.", "django.server.", "django.security."))
        message = "[REDACTED]" if raw_http or hasattr(record, "sql") else record.getMessage()
        event: dict[str, Any] = {
            "asctime": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "levelname": record.levelname,
            "name": record.name,
            "message": message,
        }
        request_id = getattr(record, "request_id", "-")
        if request_id == "-":
            event["request_id"] = "-"
        else:
            with suppress(ValueError):
                event["request_id"] = str(UUID(str(request_id)))
        if record.name == "startunnel.request" and message == REQUEST_COMPLETED:
            for field in ("route_name", "method", "status", "duration_ms"):
                value = getattr(record, field, None)
                if value is not None:
                    event[field] = value
        if record.name == "core.maintenance" and message == CYCLE_DELETION_FAILED:
            cycle_id = getattr(record, "cycle_id", None)
            if isinstance(cycle_id, UUID):
                event["cycle_id"] = str(cycle_id)
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, _value, traceback = record.exc_info
            event["exception_type"] = exc_type.__name__
            frames: list[dict[str, str | int]] = []
            while traceback is not None:
                frame = traceback.tb_frame
                frames.append(
                    {
                        "file": Path(frame.f_code.co_filename).name,
                        "function": frame.f_code.co_name,
                        "line": traceback.tb_lineno,
                    }
                )
                traceback = traceback.tb_next
            event["exception_frames"] = frames[-8:]
        return json.dumps(event, ensure_ascii=False)


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True
