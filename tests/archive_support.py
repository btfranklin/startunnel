"""Shared deterministic values for protected-archive tests."""

from __future__ import annotations

import base64

TEST_ARCHIVE_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
