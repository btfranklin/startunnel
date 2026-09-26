"""Forwarded client data is valid only from one configured proxy network."""

from __future__ import annotations

from typing import Any

from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from api.router import _source_ip
from core.middleware import TrustedProxyMiddleware


@override_settings(STARTUNNEL_TRUSTED_PROXY_CIDRS=["10.0.0.0/8"])
def test_untrusted_peer_cannot_spoof_forwarded_client_address() -> None:
    captured: dict[str, Any] = {}

    def endpoint(request: Any) -> HttpResponse:
        captured["source_ip"] = _source_ip(request)
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
        captured["source_ip"] = _source_ip(request)
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
