"""ASGI entry point."""

import os
from typing import cast

from asgiref.typing import ASGI3Application
from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.asgi import get_asgi_application

from core.asgi import LifespanMiddleware, RequestBodyLimitMiddleware, StaticFilesMiddleware

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "startunnel.settings.production")
# Django uses broad dictionaries in its ASGI annotations. The handler follows ASGI 3.
django_application = cast(ASGI3Application, get_asgi_application())
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
