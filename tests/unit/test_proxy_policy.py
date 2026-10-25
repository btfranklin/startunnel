"""Forwarded client data is valid only from one configured proxy network."""

from __future__ import annotations

import pytest
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory, override_settings

from core.middleware import TrustedProxyMiddleware
from core.security import client_ip


@override_settings(STARTUNNEL_TRUSTED_PROXY_CIDRS=["10.0.0.0/8"])
def test_untrusted_peer_cannot_spoof_forwarded_client_address() -> None:
    captured: dict[str, object] = {}

    def endpoint(request: HttpRequest) -> HttpResponse:
        captured["source_ip"] = client_ip(request)
        captured["proto"] = request.META.get("HTTP_X_FORWARDED_PROTO")
        return HttpResponse()

    middleware = TrustedProxyMiddleware(endpoint)
    request = RequestFactory().get(
        "/api/v1/tunnels",
        HTTP_X_FORWARDED_FOR="198.51.100.10",
        HTTP_X_FORWARDED_PROTO="https",
        REMOTE_ADDR="203.0.113.8",
    )

    middleware(request)

    assert captured == {"source_ip": "203.0.113.8", "proto": None}


@override_settings(STARTUNNEL_TRUSTED_PROXY_CIDRS=["10.0.0.0/8"])
def test_trusted_peer_can_supply_one_replaced_client_address() -> None:
    captured: dict[str, object] = {}

    def endpoint(request: HttpRequest) -> HttpResponse:
        captured["source_ip"] = client_ip(request)
        captured["proto"] = request.META.get("HTTP_X_FORWARDED_PROTO")
        return HttpResponse()

    middleware = TrustedProxyMiddleware(endpoint)
    request = RequestFactory().get(
        "/api/v1/tunnels",
        HTTP_X_FORWARDED_FOR="198.51.100.10",
        HTTP_X_FORWARDED_PROTO="https",
        REMOTE_ADDR="10.4.3.2",
    )

    middleware(request)

    assert captured == {"source_ip": "198.51.100.10", "proto": "https"}


@override_settings(STARTUNNEL_TRUSTED_PROXY_CIDRS=["10.0.0.0/8"])
def test_login_limit_uses_the_client_address_from_a_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from accounts.forms import ThrottledAuthenticationForm

    sources: list[str] = []
    monkeypatch.setattr(
        "core.rate_limits.consume_login_attempt",
        lambda **kwargs: sources.append(kwargs["source_ip"]),
    )
    monkeypatch.setattr("django.contrib.auth.forms.authenticate", lambda *args, **kwargs: None)

    def endpoint(request: HttpRequest) -> HttpResponse:
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


@pytest.mark.parametrize("peer", ["172.18.0.1", "172.30.254.2", "172.30.254.253"])
@override_settings(
    STARTUNNEL_TRUSTED_PROXY_CIDRS=["127.0.0.0/8", "172.30.254.254/32"],
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
)
def test_bridge_gateway_and_neighbor_cannot_spoof_forwarded_claims(peer: str) -> None:
    for source in ("198.51.100.10", "198.51.100.11"):
        request = RequestFactory().post(
            "/accounts/login/",
            REMOTE_ADDR=peer,
            HTTP_X_FORWARDED_FOR=source,
            HTTP_X_FORWARDED_PROTO="https",
            HTTP_X_FORWARDED_HOST="attacker.invalid",
            HTTP_X_FORWARDED_PORT="443",
            HTTP_FORWARDED="for=198.51.100.10;proto=https",
        )
        TrustedProxyMiddleware(lambda request: HttpResponse())(request)
        assert client_ip(request) == peer
        assert not request.is_secure()
        assert all(field not in request.META for field in TrustedProxyMiddleware.forwarded_fields)


@override_settings(
    STARTUNNEL_TRUSTED_PROXY_CIDRS=["127.0.0.0/8", "172.30.254.254/32"],
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
)
def test_exact_proxy_peer_preserves_https_and_client_identity() -> None:
    request = RequestFactory().get(
        "/accounts/login/",
        REMOTE_ADDR="172.30.254.254",
        HTTP_X_FORWARDED_FOR="198.51.100.10",
        HTTP_X_FORWARDED_PROTO="https",
    )
    TrustedProxyMiddleware(lambda request: HttpResponse())(request)
    assert client_ip(request) == "198.51.100.10"
    assert request.is_secure()


@override_settings(STARTUNNEL_TRUSTED_PROXY_CIDRS=["127.0.0.0/8"])
def test_direct_development_login_keeps_one_limit_source(monkeypatch: pytest.MonkeyPatch) -> None:
    from accounts.forms import ThrottledAuthenticationForm

    sources: list[str] = []
    monkeypatch.setattr(
        "core.rate_limits.consume_login_attempt",
        lambda **kwargs: sources.append(kwargs["source_ip"]),
    )
    monkeypatch.setattr("django.contrib.auth.forms.authenticate", lambda *args, **kwargs: None)
    for forwarded in ("198.51.100.10", "198.51.100.11"):
        request = RequestFactory().post(
            "/accounts/login/",
            REMOTE_ADDR="172.18.0.1",
            HTTP_X_FORWARDED_FOR=forwarded,
        )
        TrustedProxyMiddleware(lambda request: HttpResponse())(request)
        form = ThrottledAuthenticationForm(
            request, data={"username": "operator", "password": "bad"}
        )
        assert not form.is_valid()
    assert sources == ["172.18.0.1", "172.18.0.1"]
