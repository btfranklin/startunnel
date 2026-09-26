"""Fast local test settings.

Set TEST_DATABASE_URL to PostgreSQL for integration and concurrency tests.
"""

import os

import dj_database_url

from .base import *  # noqa: F403

# This value is only for isolated tests and cannot authorize a deployed service.
SECRET_KEY = "test-only-secret-key"  # nosec B105
STARTUNNEL_ENV = "test"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
if test_database_url := os.getenv("TEST_DATABASE_URL"):
    DATABASES = {"default": dj_database_url.parse(test_database_url)}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
