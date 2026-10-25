"""Agent access keeps creator IDs without loading unused credential rows."""

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from tests.factories import CredentialFactory

from tunnels.access import resolve_tunnel
from tunnels.cycles import create_tunnel

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("for_update", [False, True])
def test_agent_access_does_not_load_the_creator(
    credential_factory: CredentialFactory,
    monkeypatch: pytest.MonkeyPatch,
    fallback: bool,
    for_update: bool,
) -> None:
    creator, _ = credential_factory()
    reader, _ = credential_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="access-without-creator",
        root_content={"type": "text", "text": "Shared work."},
    )
    if fallback:
        monkeypatch.setattr(
            "tunnels.access._resolve_current_tunnel_if_authorized", lambda **kwargs: None
        )
    with transaction.atomic(), CaptureQueriesContext(connection) as queries:
        tunnel = resolve_tunnel(credential=reader, address=created.address, for_update=for_update)
    assert tunnel.id == created.tunnel.id
    assert tunnel.creator_id == creator.id
    assert "creator" not in tunnel._state.fields_cache
    assert all('JOIN "agents_agentcredential"' not in query["sql"] for query in queries)
