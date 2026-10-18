"""Production settings for the Docker deployment."""

from urllib.parse import urlparse

from .base import *  # noqa: F403

SECRET_KEY = env("STARTUNNEL_SECRET_KEY", required=True)  # noqa: F405
if len(SECRET_KEY) < 50:
    raise RuntimeError("STARTUNNEL_SECRET_KEY must contain at least 50 characters.")


def required_text_secret(name: str) -> str:
    """Require one printable ASCII value for production secret settings."""

    value = env(name, required=True)  # noqa: F405
    if len(value) < 32:
        raise RuntimeError(f"{name} must contain at least 32 characters.")
    if not value.isascii() or not value.isprintable():
        raise RuntimeError(f"{name} must contain printable single-line ASCII text.")
    return value


API_KEY_PEPPER = required_text_secret("STARTUNNEL_API_KEY_PEPPER")
IDEMPOTENCY_SECRET = required_text_secret("STARTUNNEL_IDEMPOTENCY_SECRET")
METRICS_TOKEN = required_text_secret("STARTUNNEL_METRICS_TOKEN")
if (
    len(
        {
            API_KEY_PEPPER,
            IDEMPOTENCY_SECRET,
            METRICS_TOKEN,
        }
    )
    != 3
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
    origin.strip().rstrip("/")
    for origin in env("STARTUNNEL_CSRF_TRUSTED_ORIGINS", required=True).split(",")  # noqa: F405
    if origin.strip()
]
STARTUNNEL_BASE_URL = env("STARTUNNEL_BASE_URL", required=True)  # noqa: F405
public_url = urlparse(STARTUNNEL_BASE_URL)
if (
    public_url.scheme not in {"http", "https"}
    or not public_url.hostname
    or public_url.username
    or public_url.password
    or public_url.path not in {"", "/"}
    or public_url.query
    or public_url.fragment
):
    raise RuntimeError("STARTUNNEL_BASE_URL must be one complete HTTP or HTTPS origin.")
public_host = public_url.hostname.lower()
if public_host not in {host.lower() for host in ALLOWED_HOSTS}:
    raise RuntimeError("STARTUNNEL_BASE_URL host must be in STARTUNNEL_ALLOWED_HOSTS.")
public_origin = f"{public_url.scheme}://{public_url.netloc}"
if public_origin not in CSRF_TRUSTED_ORIGINS:
    raise RuntimeError("STARTUNNEL_BASE_URL origin must be in STARTUNNEL_CSRF_TRUSTED_ORIGINS.")
STARTUNNEL_BASE_URL = public_origin

DEBUG = False
https_origin = public_url.scheme == "https"
SECURE_SSL_REDIRECT = env_bool("STARTUNNEL_SECURE_SSL_REDIRECT", https_origin)  # noqa: F405
SESSION_COOKIE_SECURE = https_origin
CSRF_COOKIE_SECURE = https_origin
SECURE_HSTS_SECONDS = 31_536_000 if https_origin else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = https_origin
SECURE_HSTS_PRELOAD = https_origin
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
