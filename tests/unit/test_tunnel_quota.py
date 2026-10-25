"""Tunnel creation caps limit one credential without changing shared access."""

from dataclasses import replace

import pytest
from tests.factories import CredentialFactory, UserFactory

from agents.models import AgentCredential
from core.errors import QuotaExceeded
from core.limits import Limits
from tunnels.admin_actions import retire_tunnel_as_admin, retirement_confirmation
from tunnels.cycles import (
    CreatedTunnel,
    agent_tunnel_status,
    close_cycle,
    create_tunnel,
    start_cycle,
)
from tunnels.messages import post_reply, read_tree

pytestmark = pytest.mark.django_db


def _create(credential: AgentCredential, key: str) -> CreatedTunnel:
    return create_tunnel(
        credential=credential,
        idempotency_key=key,
        root_content={"type": "text", "text": "Shared work."},
    )


def test_creation_cap_counts_each_creator_and_preserves_exact_retries(
    credential_factory: CredentialFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = replace(Limits(), non_retired_tunnels=2, non_retired_tunnels_per_credential=1)
    monkeypatch.setattr("tunnels.cycles.instance_limits.effective_limits", lambda: limits)
    first, _ = credential_factory()
    second, _ = credential_factory()
    third, _ = credential_factory()
    created = _create(first, "quota-first-create")

    replay = _create(first, "quota-first-create")
    assert replay.replay
    assert replay.tunnel.id == created.tunnel.id
    with pytest.raises(QuotaExceeded, match="credential"):
        _create(first, "quota-first-blocked")
    assert not first.agent_receipts.filter(resource_id__isnull=True).exists()

    _create(second, "quota-second-create")
    status = agent_tunnel_status(credential=first)
    assert status.tunnels == 2
    assert status.created_tunnels == 1
    assert _create(first, "quota-first-create").replay
    with pytest.raises(QuotaExceeded, match="instance"):
        _create(third, "quota-instance-blocked")


def test_creation_cap_preserves_shared_use_and_retirement_releases_capacity(
    credential_factory: CredentialFactory,
    user_factory: UserFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = replace(Limits(), non_retired_tunnels_per_credential=1)
    monkeypatch.setattr("tunnels.cycles.instance_limits.effective_limits", lambda: limits)
    creator, _ = credential_factory()
    reader, _ = credential_factory()
    created = _create(creator, "quota-reuse-create")
    post_reply(
        credential=reader,
        idempotency_key="quota-shared-reply",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Shared reply."},
    )
    assert len(read_tree(credential=reader, address=created.address).messages) == 2
    close_cycle(
        credential=creator,
        idempotency_key="quota-reuse-close",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    with pytest.raises(QuotaExceeded, match="credential"):
        _create(creator, "quota-dormant-blocked")
    started = start_cycle(
        credential=creator,
        idempotency_key="quota-reuse-start",
        address=created.address,
        expected_address_generation=1,
        root_content={"type": "text", "text": "More shared work."},
    )
    assert started.cycle.number == 2
    retire_tunnel_as_admin(
        actor=user_factory(),
        tunnel_id=created.tunnel.id,
        expected_address_generation=1,
        confirmation=retirement_confirmation(created.tunnel),
    )
    assert agent_tunnel_status(credential=creator).created_tunnels == 0
    replacement = _create(creator, "quota-replacement-create")
    assert replacement.tunnel.id != created.tunnel.id
    assert agent_tunnel_status(credential=creator).created_tunnels == 1
