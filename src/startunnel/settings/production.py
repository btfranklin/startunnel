"""Production settings for the Docker deployment."""

import base64
import binascii
from urllib.parse import urlparse

from .base import *  # noqa: F403

SECRET_KEY = env("STARTUNNEL_SECRET_KEY", required=True)  # noqa: F405
if len(SECRET_KEY) < 50:
    raise RuntimeError("STARTUNNEL_SECRET_KEY must contain at least 50 characters.")


def required_32_byte_secret(name: str) -> str:
    value = env(name, required=True)  # noqa: F405
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise RuntimeError(f"{name} must be URL-safe base64 without padding.") from error
    if len(decoded) != 32:
        raise RuntimeError(f"{name} must encode exactly 32 random bytes.")
    return value


API_KEY_PEPPER = required_32_byte_secret("STARTUNNEL_API_KEY_PEPPER")
ADDRESS_SECRET = required_32_byte_secret("STARTUNNEL_ADDRESS_SECRET")
ADDRESS_DERIVATION_SECRET = required_32_byte_secret("STARTUNNEL_ADDRESS_DERIVATION_SECRET")
IDEMPOTENCY_SECRET = required_32_byte_secret("STARTUNNEL_IDEMPOTENCY_SECRET")
METRICS_TOKEN = required_32_byte_secret("STARTUNNEL_METRICS_TOKEN")
if (
    len(
        {
            API_KEY_PEPPER,
            ADDRESS_SECRET,
            ADDRESS_DERIVATION_SECRET,
            IDEMPOTENCY_SECRET,
            METRICS_TOKEN,
        }
    )
    != 5
):
    raise RuntimeError("Each StarTunnel cryptographic secret must be different.")
DATABASES = {
    "default": database_configuration(  # noqa: F405
        env("DATABASE_URL", required=True)  # noqa: F405
    )
}
if DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
    raise RuntimeError("DATABASE_URL must use PostgreSQL in production.")
ALLOWED_HOSTS = [
    host.strip()
    for host in env("STARTUNNEL_ALLOWED_HOSTS", required=True).split(",")  # noqa: F405
    if host.strip()
]
if not ALLOWED_HOSTS:
    raise RuntimeError("STARTUNNEL_ALLOWED_HOSTS must contain at least one host.")
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in env("STARTUNNEL_CSRF_TRUSTED_ORIGINS", required=True).split(",")  # noqa: F405
    if origin.strip()
]
STARTUNNEL_BASE_URL = env("STARTUNNEL_BASE_URL", required=True)  # noqa: F405
public_url = urlparse(STARTUNNEL_BASE_URL)
if (
    public_url.scheme != "https"
    or not public_url.hostname
    or public_url.username
    or public_url.password
    or public_url.path not in {"", "/"}
    or public_url.query
    or public_url.fragment
):
    raise RuntimeError("STARTUNNEL_BASE_URL must be one complete HTTPS origin.")
public_host = public_url.hostname.lower()
if public_host not in {host.lower() for host in ALLOWED_HOSTS}:
    raise RuntimeError("STARTUNNEL_BASE_URL host must be in STARTUNNEL_ALLOWED_HOSTS.")
public_origin = f"https://{public_url.netloc}"
if public_origin not in CSRF_TRUSTED_ORIGINS:
    raise RuntimeError("STARTUNNEL_BASE_URL origin must be in STARTUNNEL_CSRF_TRUSTED_ORIGINS.")
STARTUNNEL_BASE_URL = public_origin

DEBUG = False
SECURE_SSL_REDIRECT = env_bool("STARTUNNEL_SECURE_SSL_REDIRECT", True)  # noqa: F405
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
