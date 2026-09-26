"""Local development settings."""

from .base import *  # noqa: F403

DEBUG = True
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
}
