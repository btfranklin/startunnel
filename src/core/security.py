"""Shared HTTP response security values."""

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
    "form-action 'self'; img-src 'self' data:; font-src 'self'; "
    "style-src 'self'; script-src 'self'; connect-src 'self'; "
    "object-src 'none'; media-src 'self'"
)
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=()"
