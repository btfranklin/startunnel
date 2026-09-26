"""ASGI entry point."""

import os

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.asgi import get_asgi_application

from core.asgi import LifespanMiddleware, RequestBodyLimitMiddleware, StaticFilesMiddleware

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "startunnel.settings.production")
django_application = get_asgi_application()
bounded_application = RequestBodyLimitMiddleware(
    django_application,
    maximum_bytes=settings.STARTUNNEL_MAX_REQUEST_BODY_BYTES,
)
application = LifespanMiddleware(
    StaticFilesMiddleware(
        bounded_application,
        root=settings.STATIC_ROOT,
        url_prefix=settings.STATIC_URL,
        find_file=finders.find if settings.DEBUG else None,
    )
)
