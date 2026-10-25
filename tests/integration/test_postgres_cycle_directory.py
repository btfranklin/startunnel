"""PostgreSQL returns bounded cycle previews without loading root payloads."""

from __future__ import annotations

import json

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from tests.factories import CredentialFactory

from api.router import _cycle_directory_item
from tunnels.cycles import close_cycle, create_tunnel, list_cycles, start_cycle

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def test_cycle_directory_bounds_root_content_before_model_loading(
    credential_factory: CredentialFactory,
) -> None:
    creator, _ = credential_factory()
    text = "x" * 65536
    created = create_tunnel(
        credential=creator,
        idempotency_key="pg-cycle-preview-create",
        root_content={"type": "text", "text": text},
    )
    close_cycle(
        credential=creator,
        idempotency_key="pg-cycle-preview-close",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    value = "🜣" * 16383 + "xx"
    json_text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert len(json_text.encode("utf-8")) == 65536
    started = start_cycle(
        credential=creator,
        idempotency_key="pg-cycle-preview-start",
        address=created.address,
        expected_address_generation=1,
        root_content={"type": "json", "value": value},
    )

    page = list_cycles(credential=creator, address=created.address)

    assert [cycle.id for cycle in page.cycles] == [started.cycle.id, created.cycle.id]
    assert page.snapshot_number == 2
    assert page.next_before_number is None
    assert page.complete is True
    for cycle, raw in zip(page.cycles, (json_text, text), strict=True):
        assert "creator" not in cycle._state.fields_cache
        root = cycle.root_message
        assert root is not None
        assert root.get_deferred_fields() == {"text_payload", "json_payload"}
        assert getattr(cycle, "_root_content_preview", None) == raw[:281]
        with CaptureQueriesContext(connection) as queries:
            item = _cycle_directory_item(cycle)
        assert len(queries) == 0
        assert item.root.id == root.id
        assert item.root.content_preview == raw[:279] + "…"
        assert item.message_count == 1
        assert item.content_bytes == 65536
        assert item.state == cycle.state
        assert item.closed_at == cycle.closed_at
        assert item.delete_after == cycle.delete_after
