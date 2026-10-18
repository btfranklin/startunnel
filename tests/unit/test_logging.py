"""JSON logs keep operational diagnostics and omit raw request and SQL values."""

import json
import logging
import sys
from typing import Any
from uuid import uuid4

import pytest
from django.http import HttpResponse
from django.test import Client, RequestFactory

from core.logging import SafeJsonFormatter
from core.metrics import API_REQUEST_SECONDS
from core.middleware import RequestIdMiddleware


@pytest.mark.parametrize(
    "logger_name", ["django.request", "django.server", "django.security.DisallowedHost"]
)
def test_json_formatter_cannot_serialize_raw_http_tokens(logger_name: str) -> None:
    token = "raw-access-token"
    confirmation = "raw-authorization-code"
    host = "synthetic-host-marker"
    record = logging.LogRecord(
        name=logger_name,
        level=logging.WARNING,
        pathname="/safe/source.py",
        lineno=42,
        msg=(
            f"Request /continue/?token={token} failed after /continue/?code={confirmation}; "
            f"Host: {host}"
        ),
        args=(),
        exc_info=None,
    )
    record.request_path = f"/continue/?token={token}"
    record.request = RequestFactory().get(f"/continue/?code={confirmation}")
    output = SafeJsonFormatter().format(record)
    parsed = json.loads(output)
    assert token not in output
    assert confirmation not in output
    assert host not in output
    assert "[REDACTED]" in parsed["message"]
    assert "request_path" not in parsed
    assert "request" not in parsed


def test_operational_warning_survives_but_sql_query_text_does_not() -> None:
    warning = logging.LogRecord(
        name="django.db.backends",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="Connection pool exhausted.",
        args=(),
        exc_info=None,
    )
    warning.untrusted_extra = "synthetic-secret-marker"
    rendered_warning = SafeJsonFormatter().format(warning)
    assert json.loads(rendered_warning)["message"] == "Connection pool exhausted."
    assert "synthetic-secret-marker" not in rendered_warning

    query = logging.LogRecord(
        name="django.db.backends",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg="SELECT * FROM messages WHERE content = 'synthetic-payload-marker'",
        args=(),
        exc_info=None,
    )
    query.sql = "SELECT * FROM messages WHERE content = 'synthetic-payload-marker'"
    rendered_query = SafeJsonFormatter().format(query)
    assert json.loads(rendered_query)["message"] == "[REDACTED]"
    assert "synthetic-payload-marker" not in rendered_query


def test_json_formatter_keeps_exception_type_and_location_without_values() -> None:
    exception_token = "synthetic-message-body-marker"
    stack_token = "synthetic-cursor-marker"
    address_token = "synthetic-glyph-address-marker"
    key_token = "synthetic-agent-key-marker"
    try:
        raise ValueError(f"failed at /accounts/github/login/callback/?code={exception_token}")
    except ValueError:
        exception_info = sys.exc_info()
    record = logging.LogRecord(
        name="django.request",
        level=logging.ERROR,
        pathname="/safe/source.py",
        lineno=42,
        msg=f"Request failed with {key_token}.",
        args=(),
        exc_info=exception_info,
    )
    record.stack_info = f"stack at /app/teams/invitations/{stack_token}/"
    record.context = {
        "address": address_token,
        "nested": [b"opaque-bytes", {"snapshot_cursor": stack_token}],
    }

    output = SafeJsonFormatter().format(record)

    assert exception_token not in output
    assert stack_token not in output
    assert address_token not in output
    assert key_token not in output
    assert "opaque-bytes" not in output
    parsed = json.loads(output)
    assert parsed["message"] == "[REDACTED]"
    assert parsed["exception_type"] == "ValueError"
    assert exception_info[2] is not None
    assert parsed["exception_frames"][-1] == {
        "file": "test_logging.py",
        "function": "test_json_formatter_keeps_exception_type_and_location_without_values",
        "line": exception_info[2].tb_lineno,
    }
    assert "exc_info" not in parsed
    assert "stack_info" not in parsed
    assert "context" not in parsed


def test_fixed_listener_warning_survives_formatter() -> None:
    record = logging.LogRecord(
        name="core.activity_listener",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="The PostgreSQL activity listener disconnected.",
        args=(),
        exc_info=None,
    )
    record.context = {"address": "sensitive-glyph-marker"}
    output = SafeJsonFormatter().format(record)
    assert json.loads(output)["message"] == "The PostgreSQL activity listener disconnected."
    assert "sensitive-glyph-marker" not in output


def test_fixed_maintenance_failure_keeps_cycle_id_and_safe_exception() -> None:
    cycle_id = uuid4()
    try:
        raise RuntimeError("sensitive SQL parameter marker")
    except RuntimeError:
        exception_info = sys.exc_info()
    record = logging.LogRecord(
        name="tunnels.maintenance",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Cycle content deletion failed.",
        args=(),
        exc_info=exception_info,
    )
    record.cycle_id = cycle_id
    output = SafeJsonFormatter().format(record)
    parsed = json.loads(output)
    assert parsed["message"] == "Cycle content deletion failed."
    assert parsed["cycle_id"] == str(cycle_id)
    assert parsed["exception_type"] == "RuntimeError"
    assert "sensitive SQL parameter marker" not in output


def test_request_logger_normalizes_an_unknown_method(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "st_" + "A" * 43
    request = RequestFactory().generic(marker, "/safe-path/")
    request.resolver_match = None

    with caplog.at_level(logging.INFO, logger="startunnel.request"):
        response = RequestIdMiddleware(lambda value: HttpResponse())(request)

    assert response.status_code == 200
    record = next(record for record in caplog.records if record.name == "startunnel.request")
    assert getattr(record, "method", None) == "OTHER"
    formatted = SafeJsonFormatter().format(record)
    assert marker not in formatted


@pytest.mark.django_db
def test_api_lifecycle_logs_only_safe_request_metadata(
    client: Client,
    credential_factory: Any,
    user_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile_marker = f"marker-{uuid4().hex}"
    payload_marker = f"payload-marker-{uuid4().hex}"
    sender_user = user_factory(username=profile_marker)
    sender, sender_key = credential_factory(user=sender_user, name="Logging sender")
    _, receiver_key = credential_factory(name="Logging receiver")
    monkeypatch.setattr("api.auth.consume_api_operation", lambda credential, limits: None)

    def post(
        path: str,
        body: dict[str, Any],
        key: str,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return client.post(
            path,
            data=json.dumps(body),
            content_type="application/json",
            headers=headers,
        )

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="startunnel.request"):
        created_response = post(
            "/api/v1/tunnels",
            {
                "label": "Logging proof",
                "cycle": {
                    "label": "Safe metadata",
                    "expires_in_seconds": 3600,
                    "root": {
                        "content": {"type": "text", "text": payload_marker},
                        "correlation_id": profile_marker,
                    },
                },
            },
            sender_key,
            idempotency_key=f"logging-create-{uuid4().hex}",
        )
        assert created_response.status_code == 201
        address = created_response.json()["tunnel"]["address"]
        cycle_id = created_response.json()["cycle"]["id"]
        root_id = created_response.json()["cycle"]["root"]["id"]
        reply_response = post(
            "/api/v1/messages",
            {
                "address": address,
                "parent_id": root_id,
                "content": {"type": "text", "text": "safe reply"},
                "correlation_id": profile_marker,
            },
            receiver_key,
            idempotency_key=f"logging-reply-{uuid4().hex}",
        )
        assert reply_response.status_code == 201
        reply_id = reply_response.json()["message"]["id"]
        tree_response = post(
            "/api/v1/tree",
            {"address": address, "cycle_id": cycle_id},
            receiver_key,
        )
        assert tree_response.status_code == 200
        snapshot_cursor = tree_response.json()["snapshot_cursor"]
        closed = post(
            "/api/v1/cycles/close",
            {"address": address, "expected_cycle_id": cycle_id},
            sender_key,
            idempotency_key=f"logging-close-{uuid4().hex}",
        )
        assert closed.status_code == 204

    records = [record for record in caplog.records if record.name == "startunnel.request"]
    assert len(records) == 4
    serialized = json.dumps(
        [record.__dict__ for record in records],
        ensure_ascii=False,
        default=str,
    )
    for sensitive in [
        sender_key,
        receiver_key,
        address,
        cycle_id,
        root_id,
        reply_id,
        snapshot_cursor,
        payload_marker,
        profile_marker,
        sender.created_by.username,
    ]:
        assert sensitive not in serialized
    for record in records:
        route_name = str(getattr(record, "route_name", ""))
        method = str(getattr(record, "method", ""))
        status = int(getattr(record, "status", 0))
        duration_ms = float(getattr(record, "duration_ms", -1))
        assert record.getMessage() == "Request completed."
        assert route_name
        assert method == "POST"
        assert status in {200, 201, 204}
        assert duration_ms >= 0
        assert "path" not in record.__dict__
        assert "body" not in record.__dict__

    histogram_samples = [
        sample
        for metric in API_REQUEST_SECONDS.collect()
        for sample in metric.samples
        if sample.name == "startunnel_api_request_seconds_count"
    ]
    for record in records:
        route_name = str(getattr(record, "route_name", ""))
        status = int(getattr(record, "status", 0))
        assert any(
            sample.labels
            == {
                "operation": route_name,
                "status_class": f"{status // 100}xx",
            }
            and sample.value >= 1
            for sample in histogram_samples
        )
