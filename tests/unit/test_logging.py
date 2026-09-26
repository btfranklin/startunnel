"""JSON logs remove browser bearer tokens before serialization."""

import json
import logging
import sys
from typing import Any
from uuid import uuid4

import pytest
from django.http import HttpResponse
from django.test import Client, RequestFactory

from core.logging import RedactingJsonFormatter, redact_sensitive_urls
from core.metrics import API_REQUEST_SECONDS
from core.middleware import RequestIdMiddleware


@pytest.mark.parametrize(
    ("path", "secret"),
    [
        ("/continue/?code=authorization-code-secret", "authorization-code-secret"),
        ("/continue/?state=authorization-state-secret", "authorization-state-secret"),
        ("/continue/?token=query-token-secret&next=/app/", "query-token-secret"),
        ("/continue/?key=query-key-secret&next=/app/", "query-key-secret"),
    ],
)
def test_browser_bearer_tokens_are_redacted(path: str, secret: str) -> None:
    redacted = redact_sensitive_urls(path)
    assert secret not in redacted
    assert "[REDACTED]" in redacted


def test_json_formatter_cannot_serialize_raw_tokens() -> None:
    token = "raw-access-token"
    confirmation = "raw-authorization-code"
    record = logging.LogRecord(
        name="django.request",
        level=logging.WARNING,
        pathname="/safe/source.py",
        lineno=42,
        msg=(f"Request /continue/?token={token} failed after /continue/?code={confirmation}"),
        args=(),
        exc_info=None,
    )
    record.request_path = f"/continue/?token={token}"
    record.request = RequestFactory().get(f"/continue/?code={confirmation}")
    output = RedactingJsonFormatter().format(record)
    parsed = json.loads(output)
    assert token not in output
    assert confirmation not in output
    assert "[REDACTED]" in parsed["message"]
    assert "request_path" not in parsed
    assert "request" not in parsed


def test_json_formatter_omits_exception_stack_and_arbitrary_values() -> None:
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

    output = RedactingJsonFormatter().format(record)

    assert exception_token not in output
    assert stack_token not in output
    assert address_token not in output
    assert key_token not in output
    assert "opaque-bytes" not in output
    parsed = json.loads(output)
    assert parsed["message"] == "[REDACTED]"
    assert "exc_info" not in parsed
    assert "stack_info" not in parsed
    assert "context" not in parsed


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
    formatted = RedactingJsonFormatter().format(record)
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
    monkeypatch.setattr(
        "api.router.consume_tunnel_creation",
        lambda credential, limits: None,
    )

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
