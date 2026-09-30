"""Every Django middleware keeps long-poll requests on the ASGI path."""

from __future__ import annotations

import json
from typing import Any

import pytest
from asgiref.sync import iscoroutinefunction
from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory
from django.utils.module_loading import import_string

from api.middleware import ApiErrorEnvelopeMiddleware
from core.middleware import (
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    TrustedProxyMiddleware,
)


def test_entire_django_middleware_chain_is_async_capable() -> None:
    incapable = [
        path
        for path in settings.MIDDLEWARE
        if not getattr(import_string(path), "async_capable", False)
    ]

    assert incapable == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "middleware_type",
    [
        TrustedProxyMiddleware,
        RequestIdMiddleware,
        SecurityHeadersMiddleware,
        ApiErrorEnvelopeMiddleware,
    ],
)
async def test_project_middleware_awaits_an_async_downstream(middleware_type: Any) -> None:
    calls = 0

    async def endpoint(request: Any) -> HttpResponse:
        nonlocal calls
        calls += 1
        return HttpResponse("ready")

    middleware = middleware_type(endpoint)
    request = RequestFactory().get("/health/live", REMOTE_ADDR="127.0.0.1")

    assert iscoroutinefunction(middleware)
    response = await middleware(request)
    assert response.status_code == 200
    assert calls == 1


@pytest.mark.asyncio
async def test_async_api_error_replacement_keeps_the_request_id() -> None:
    async def endpoint(request: Any) -> HttpResponse:
        del request
        return HttpResponse("not found", status=404, content_type="text/html")

    request = RequestFactory().get("/api/v1/not-a-route")
    request.request_id = "safe-request-id"  # type: ignore[attr-defined]
    middleware = ApiErrorEnvelopeMiddleware(endpoint)

    response = await middleware(request)

    assert response.status_code == 404
    assert json.loads(response.content)["error"] == {
        "code": "invalid_request",
        "message": "This API route is not available.",
        "request_id": "safe-request-id",
        "field_errors": [],
    }
