"""Public documentation is fixed, safe Markdown with tested example includes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from django.http import FileResponse
from django.test import Client, override_settings

from site_app.docs import DOCUMENTATION_PAGES, _example_region, _render_cached

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.django_db
def test_each_documentation_route_renders(client: Client) -> None:
    for page in DOCUMENTATION_PAGES:
        path = "/docs/" if page.slug == "index" else f"/docs/{page.slug}/"
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Content-Security-Policy" in response


def test_raw_html_is_escaped(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.md"
    source.write_text("# Safe heading\n\n<script>alert('no')</script>\n", encoding="utf-8")
    rendered = str(_render_cached(str(source), source.stat().st_mtime_ns))
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_agent_client_download_is_exact_allowlisted_attachment(client: Client) -> None:
    response = client.get("/downloads/star_tunnel.py")
    download = cast(FileResponse, response)

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/x-python")
    assert response["Content-Disposition"] == 'attachment; filename="star_tunnel.py"'
    assert response["X-Content-Type-Options"] == "nosniff"
    assert (
        b"".join(cast(Iterator[bytes], download.streaming_content))
        == (ROOT / "cli/star_tunnel.py").read_bytes()
    )


@override_settings(PROJECT_ROOT=ROOT)
def test_example_include_cannot_leave_example_directory() -> None:
    with pytest.raises(ValueError, match="does not exist"):
        _example_region("../.env", "secret")


def test_documented_fields_exist_in_openapi() -> None:
    schema_text = (ROOT / "generated/openapi.json").read_text(encoding="utf-8")
    schema = json.loads(schema_text)

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value).union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value))
        return set()

    schema_keys = keys(schema)
    required_fields = {
        "address",
        "activity_cursor",
        "content",
        "correlation_id",
        "current_cycle",
        "cycle",
        "cycle_id",
        "cycle_number",
        "depth",
        "expected_cycle_id",
        "idempotent_replay",
        "leaf_id",
        "message_id",
        "mentions",
        "next_page_cursor",
        "nodes",
        "parent_id",
        "page_cursor",
        "root",
        "root_id",
        "sequence",
        "snapshot_cursor",
        "display_address",
        "expires_at",
        "field_errors",
        "request_id",
    }
    assert required_fields <= schema_keys


def test_openapi_contains_tree_routes_and_no_relay_routes() -> None:
    schema = json.loads((ROOT / "generated/openapi.json").read_text(encoding="utf-8"))
    paths = set(schema["paths"])
    required = {
        "/api/v1/tunnels",
        "/api/v1/messages",
        "/api/v1/tree",
        "/api/v1/tree/message",
        "/api/v1/tree/branch",
        "/api/v1/tree/replies",
        "/api/v1/cycles/close",
    }
    removed = {
        "/api/v1/claims",
        "/api/v1/claims/acknowledge",
        "/api/v1/claims/release",
        "/api/v1/claims/extend",
        "/api/v1/tunnels/{tunnel_id}",
        "/api/v1/teams/{team_id}/tunnels/{tunnel_id}",
    }
    assert required <= paths
    assert removed.isdisjoint(paths)
