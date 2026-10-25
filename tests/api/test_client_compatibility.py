"""CLI version checks stop unsupported requests before domain work."""

from __future__ import annotations

import json

import pytest
from django.db import connection
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from tests.json_values import json_elements, json_members

from api.client_compatibility import (
    CLIENT_VERSION_HEADER,
    COMPATIBILITY_HEADER,
    MINIMUM_CLIENT_VERSION,
    MINIMUM_VERSION_HEADER,
    SERVER_VERSION_HEADER,
    release_version,
)
from api.middleware import ApiErrorEnvelopeMiddleware
from api.router import api
from core.build import BUILD
from core.json_types import json_object


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("v0.3.0", (0, 3, 0)),
        ("v0.10.0", (0, 10, 0)),
        ("v1.0.0", (1, 0, 0)),
        ("v0.03.0", None),
        ("v0.3", None),
        ("v0.3.0-rc1", None),
        (" v0.3.0", None),
        ("v" + "9" * 5000 + ".0.0", None),
    ],
)
def test_release_labels_are_bounded_and_compared_as_numbers(
    version: str, expected: tuple[int, int, int] | None
) -> None:
    assert release_version(version) == expected


@pytest.mark.parametrize(
    ("version", "user_agent", "status", "compatibility"),
    [
        (None, "startunnel-agent-cli", 400, "update_required"),
        (None, "startunnel-agent-cli/v0.2.0", 400, "update_required"),
        ("v0.2.0", "startunnel-agent-cli", 400, "update_required"),
        ("v0.3.0", "startunnel-agent-cli", 200, "supported"),
        ("v0.10.0", "startunnel-agent-cli", 200, "supported"),
        ("v1.0.0", "startunnel-agent-cli", 200, "supported"),
        ("unreleased", "startunnel-agent-cli", 200, "unverified"),
        (None, "custom-api-client", 200, "not_checked"),
        ("private-rejected-value", "custom-api-client", 400, "update_required"),
    ],
)
def test_sync_version_gate_and_headers(
    version: str | None, user_agent: str, status: int, compatibility: str
) -> None:
    calls = 0

    def endpoint(request: HttpRequest) -> HttpResponse:
        nonlocal calls
        calls += 1
        return HttpResponse("{}", content_type="application/json")

    headers = {"User-Agent": user_agent}
    if version is not None:
        headers[CLIENT_VERSION_HEADER] = version
    request = RequestFactory().post(
        "/api/v1/messages", data=b"invalid", content_type="application/json", headers=headers
    )
    request.request_id = "safe-request-id"  # type: ignore[attr-defined]
    response = ApiErrorEnvelopeMiddleware(endpoint)(request)
    assert isinstance(response, HttpResponse)
    assert response.status_code == status
    assert calls == (0 if status == 400 else 1)
    assert response[MINIMUM_VERSION_HEADER] == MINIMUM_CLIENT_VERSION
    assert response[COMPATIBILITY_HEADER] == compatibility
    assert response[SERVER_VERSION_HEADER] == "unreleased"
    if status == 400:
        body = json.loads(response.content)
        assert body["error"]["code"] == "invalid_request"
        assert body["error"]["request_id"] == "safe-request-id"
        assert "startunnel update" in body["error"]["message"]
        assert "private-rejected-value" not in response.content.decode()
        assert body["error"]["field_errors"][0]["field"] == CLIENT_VERSION_HEADER


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["v0.2.0", "v0.3.0"])
async def test_async_gate_does_not_await_an_unsupported_request(version: str) -> None:
    calls = 0

    async def endpoint(request: HttpRequest) -> HttpResponse:
        nonlocal calls
        calls += 1
        return HttpResponse("{}", content_type="application/json")

    request = RequestFactory().post(
        "/api/v1/beacons/wait", headers={CLIENT_VERSION_HEADER: version}
    )
    pending = ApiErrorEnvelopeMiddleware(endpoint)(request)
    assert not isinstance(pending, HttpResponse)
    response = await pending
    assert response.status_code == (400 if version == "v0.2.0" else 200)
    assert calls == (0 if version == "v0.2.0" else 1)


@pytest.mark.parametrize("path", ["/downloads/star_tunnel.py", "/health/ready", "/api/v10/me"])
def test_non_api_paths_remain_available_to_an_older_cli(path: str) -> None:
    def endpoint(request: HttpRequest) -> HttpResponse:
        return HttpResponse("available")

    request = RequestFactory().get(path, headers={"User-Agent": "startunnel-agent-cli"})
    response = ApiErrorEnvelopeMiddleware(endpoint)(request)
    assert isinstance(response, HttpResponse)
    assert response.status_code == 200
    assert MINIMUM_VERSION_HEADER not in response


def test_server_build_label_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(BUILD, "version", "v0.3.0")
    response = Client().get("/api/v1/me")
    assert response.status_code == 401
    assert response[SERVER_VERSION_HEADER] == "v0.3.0"
    assert response[COMPATIBILITY_HEADER] == "not_checked"
    monkeypatch.setitem(BUILD, "version", "custom\nprivate-value")
    response = Client().get("/api/v1/me")
    assert response[SERVER_VERSION_HEADER] == "unreleased"
    assert "private-value" not in str(response.headers)


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/api/v1/tunnels", "/api/v1/admin/agents/create"])
def test_legacy_client_is_rejected_without_database_work(path: str) -> None:
    with CaptureQueriesContext(connection) as queries:
        response = Client().post(
            path,
            data="not-json",
            content_type="application/json",
            headers={"User-Agent": "startunnel-agent-cli", "Authorization": "Bearer invalid"},
        )
    assert response.status_code == 400
    assert len(queries) == 0
    assert "startunnel update" in response.json()["error"]["message"]
    assert response["X-Request-ID"] == response.json()["error"]["request_id"]


def test_openapi_describes_version_headers_and_update_errors() -> None:
    schema = json_object(json.loads(json.dumps(api.get_openapi_schema())))
    for methods in json_members(schema["paths"]).values():
        for method, operation_value in json_members(methods).items():
            if method not in {"get", "post"}:
                continue
            operation = json_members(operation_value)
            responses = json_members(operation["responses"])
            assert "400" in responses
            assert any(
                json_members(p)["name"] == CLIENT_VERSION_HEADER
                for p in json_elements(operation["parameters"])
            )
            for response in responses.values():
                assert set(json_members(json_members(response)["headers"])) >= {
                    SERVER_VERSION_HEADER,
                    MINIMUM_VERSION_HEADER,
                    COMPATIBILITY_HEADER,
                }
