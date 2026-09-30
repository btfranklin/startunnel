"""Shared HTTP security policy and trusted client identity."""

from ipaddress import ip_address

from django.http import HttpRequest

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
    "form-action 'self'; img-src 'self' data:; font-src 'self'; "
    "style-src 'self'; script-src 'self'; connect-src 'self'; "
    "object-src 'none'; media-src 'self'"
)
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=()"


def client_ip(request: HttpRequest) -> str:
    """Use one valid client address only after the direct proxy is trusted."""

    if getattr(request, "startunnel_trusted_proxy", False):
        forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR", "")).strip()
        if forwarded:
            try:
                return str(ip_address(forwarded))
            except ValueError:
                pass
    return str(request.META.get("REMOTE_ADDR", "unknown"))
