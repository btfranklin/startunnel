"""Uvicorn uses the application's safe JSON logging configuration."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_uvicorn_logs_keep_diagnostics_without_request_or_exception_values() -> None:
    program = """
import logging
import django

django.setup()
logging.getLogger("uvicorn").info("Server ready.")
logging.getLogger("uvicorn.access").info(
    '%s - "%s %s HTTP/%s" %d',
    "127.0.0.1", "GET", "/callback?token=sensitive-query-token", "1.1", 200,
)
try:
    raise ValueError("sensitive-exception-value")
except ValueError:
    logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "startunnel.settings.test",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "sensitive-query-token" not in result.stderr
    assert "sensitive-exception-value" not in result.stderr
    events = [json.loads(line) for line in result.stderr.splitlines()]
    assert len(events) == 3
    assert events[0]["message"] == "Server ready."
    assert events[1]["message"] == "[REDACTED]"
    assert events[2]["message"] == "Exception in ASGI application"
    assert events[2]["exception_type"] == "ValueError"
    assert events[2]["exception_frames"]
