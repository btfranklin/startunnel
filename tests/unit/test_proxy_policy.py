"""Forwarded client data is valid only from one configured proxy network."""

from __future__ import annotations

from typing import Any

import pytest
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from core.middleware import TrustedProxyMiddleware
from core.security import client_ip


@override_settings(STARTUNNEL_TRUSTED_PROXY_CIDRS=["10.0.0.0/8"])
def test_untrusted_peer_cannot_spoof_forwarded_client_address() -> None:
    captured: dict[str, Any] = {}

    def endpoint(request: Any) -> HttpResponse:
        captured["source_ip"] = client_ip(request)
        captured["proto"] = request.META.get("HTTP_X_FORWARDED_PROTO")
        return HttpResponse()

    middleware = TrustedProxyMiddleware(endpoint)
    request = RequestFactory().get(
        "/api/v1/claims",
        HTTP_X_FORWARDED_FOR="198.51.100.10",
        HTTP_X_FORWARDED_PROTO="https",
        REMOTE_ADDR="203.0.113.8",
    )

    middleware(request)

    assert captured == {"source_ip": "203.0.113.8", "proto": None}


@override_settings(STARTUNNEL_TRUSTED_PROXY_CIDRS=["10.0.0.0/8"])
def test_trusted_peer_can_supply_one_replaced_client_address() -> None:
    captured: dict[str, Any] = {}

    def endpoint(request: Any) -> HttpResponse:
        captured["source_ip"] = client_ip(request)
        captured["proto"] = request.META.get("HTTP_X_FORWARDED_PROTO")
        return HttpResponse()

    middleware = TrustedProxyMiddleware(endpoint)
    request = RequestFactory().get(
        "/api/v1/claims",
        HTTP_X_FORWARDED_FOR="198.51.100.10",
        HTTP_X_FORWARDED_PROTO="https",
        REMOTE_ADDR="10.4.3.2",
    )

    middleware(request)

    assert captured == {"source_ip": "198.51.100.10", "proto": "https"}


@override_settings(STARTUNNEL_TRUSTED_PROXY_CIDRS=["10.0.0.0/8"])
def test_login_limit_uses_the_client_address_from_a_trusted_proxy(monkeypatch: Any) -> None:
    from accounts.forms import ThrottledAuthenticationForm

    sources: list[str] = []
    monkeypatch.setattr(
        "api.rate_limits.consume_login_attempt",
        lambda **kwargs: sources.append(kwargs["source_ip"]),
    )
    monkeypatch.setattr("django.contrib.auth.forms.authenticate", lambda *args, **kwargs: None)

    def endpoint(request: Any) -> HttpResponse:
        form = ThrottledAuthenticationForm(
            request, data={"username": "operator", "password": "invalid"}
        )
        assert not form.is_valid()
        return HttpResponse()

    middleware = TrustedProxyMiddleware(endpoint)
    for source in ["198.51.100.10", "198.51.100.11"]:
        middleware(
            RequestFactory().post(
                "/accounts/login/", HTTP_X_FORWARDED_FOR=source, REMOTE_ADDR="10.4.3.2"
            )
        )
    assert sources == ["198.51.100.10", "198.51.100.11"]


@pytest.mark.parametrize("forwarded", ["invalid", "198.51.100.10, 198.51.100.11", ""])
@override_settings(STARTUNNEL_TRUSTED_PROXY_CIDRS=["10.0.0.0/8"])
def test_invalid_forwarded_address_uses_the_direct_peer(forwarded: str) -> None:
    request = RequestFactory().get(
        "/accounts/login/", HTTP_X_FORWARDED_FOR=forwarded, REMOTE_ADDR="10.4.3.2"
    )
    middleware = TrustedProxyMiddleware(lambda request: HttpResponse())
    middleware(request)
    assert client_ip(request) == "10.4.3.2"
