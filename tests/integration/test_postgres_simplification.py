"""Real PostgreSQL proofs for the simplified runtime coordination paths."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import close_old_connections, connection, transaction

from accounts.models import User
from agents.models import AgentCredential
from agents.services import CredentialError, create_credential
from api.rate_limits import _consume, consume_login_attempt
from core.activity_listener import ActivityListener
from core.limits import Limits
from core.models import RateLimitBucket
from core.notifications import ACTIVITY_CHANNEL, MAINTENANCE_CHANNEL, notify
from tunnels.errors import QuotaExceeded, RateLimited
from tunnels.maintenance import ADVISORY_LOCK_ID, _close_expired_cycles
from tunnels.migrations.sql.guards import revoke_runtime_history_delete
from tunnels.models import Cycle, Tunnel
from tunnels.services import close_cycle, create_tunnel

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL coordination tests.")


def _connection_kwargs() -> dict[str, Any]:
    database = connection.settings_dict
    return {
        "dbname": database["NAME"],
        "user": database.get("USER") or None,
        "password": database.get("PASSWORD") or None,
        "host": database.get("HOST") or None,
        "port": database.get("PORT") or None,
        "connect_timeout": 5,
    }


def _consume_in_thread(*, key: str, barrier: threading.Barrier) -> str:
    close_old_connections()
    try:
        barrier.wait(timeout=15)
        try:
            _consume(key=key, limit=7, window_seconds=60, rule="concurrent")
        except RateLimited:
            return "limited"
        return "accepted"
    finally:
        close_old_connections()


def test_rate_limit_is_atomic_across_database_connections() -> None:
    workers = 20
    barrier = threading.Barrier(workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(
            executor.map(
                lambda _: _consume_in_thread(key="shared-rate-limit", barrier=barrier),
                range(workers),
            )
        )

    assert results.count("accepted") == 7
    assert results.count("limited") == workers - 7
    bucket = RateLimitBucket.objects.get()
    assert len(bucket.accepted_at_ms) == 7
    assert all(isinstance(value, int) for value in bucket.accepted_at_ms)


def _login_in_thread(*, barrier: threading.Barrier) -> str:
    close_old_connections()
    try:
        barrier.wait(timeout=15)
        try:
            consume_login_attempt(source_ip="192.0.2.90", username="SamePerson")
        except RateLimited:
            return "limited"
        return "accepted"
    finally:
        close_old_connections()


def test_login_limit_is_atomic_and_stores_only_a_digest() -> None:
    workers = 16
    barrier = threading.Barrier(workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(lambda _: _login_in_thread(barrier=barrier), range(workers)))

    assert results.count("accepted") == 10
    assert results.count("limited") == 6
    bucket = RateLimitBucket.objects.get()
    assert bytes(bucket.key_digest) not in {b"192.0.2.90", b"SamePerson"}
    assert len(bytes(bucket.key_digest)) == 32


def _create_credential_in_thread(*, actor_id: UUID, barrier: threading.Barrier) -> str:
    close_old_connections()
    try:
        actor = User.objects.get(pk=actor_id)
        barrier.wait(timeout=15)
        try:
            create_credential(actor=actor, name=f"Worker {actor_id}")
        except CredentialError:
            return "limited"
        return "created"
    finally:
        close_old_connections()


def test_instance_credential_quota_cannot_be_exceeded_concurrently(
    user_factory: Any,
    settings: Any,
) -> None:
    settings.STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT = 1
    actors = [user_factory(username=f"quota-user-{index}") for index in range(4)]
    barrier = threading.Barrier(len(actors))
    with ThreadPoolExecutor(max_workers=len(actors)) as executor:
        results = list(
            executor.map(
                lambda actor: _create_credential_in_thread(
                    actor_id=actor.id,
                    barrier=barrier,
                ),
                actors,
            )
        )

    assert results.count("created") == 1
    assert results.count("limited") == 3
    assert AgentCredential.objects.filter(revoked_at__isnull=True).count() == 1


def _create_tunnel_in_thread(
    *, credential_id: UUID, barrier: threading.Barrier, position: int
) -> str:
    close_old_connections()
    try:
        credential = AgentCredential.objects.get(pk=credential_id)

        def wait_for_start() -> None:
            barrier.wait(timeout=15)

        try:
            create_tunnel(
                credential=credential,
                idempotency_key=f"instance-quota-{position}",
                root_content={"type": "text", "text": "One instance quota."},
                new_request_gate=wait_for_start,
            )
        except QuotaExceeded:
            return "limited"
        return "created"
    finally:
        close_old_connections()


def test_instance_tunnel_quota_cannot_be_exceeded_concurrently(
    credential_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = [credential_factory(name=f"Quota agent {index}")[0] for index in range(4)]
    limits = replace(Limits(), non_retired_tunnels=1)
    monkeypatch.setattr("tunnels.services.limits_for", lambda credential: limits)
    barrier = threading.Barrier(len(credentials))
    with ThreadPoolExecutor(max_workers=len(credentials)) as executor:
        results = list(
            executor.map(
                lambda item: _create_tunnel_in_thread(
                    credential_id=item[1].id,
                    barrier=barrier,
                    position=item[0],
                ),
                enumerate(credentials),
            )
        )

    assert results.count("created") == 1
    assert results.count("limited") == 3
    assert Tunnel.objects.exclude(state=Tunnel.State.RETIRED).count() == 1


def test_notifications_follow_commit_and_rollback() -> None:
    with psycopg.connect(**_connection_kwargs(), autocommit=True) as listener:
        listener.execute(f"LISTEN {ACTIVITY_CHANNEL}")
        with transaction.atomic():
            notify(ACTIVITY_CHANNEL, "committed")
            assert list(listener.notifies(timeout=0.1, stop_after=1)) == []
        committed = list(listener.notifies(timeout=2, stop_after=1))
        assert [item.payload for item in committed] == ["committed"]

        with pytest.raises(RuntimeError, match="rollback"), transaction.atomic():
            notify(ACTIVITY_CHANNEL, "rolled-back")
            raise RuntimeError("rollback")
        assert list(listener.notifies(timeout=0.2, stop_after=1)) == []


def test_bulk_lifecycle_events_wake_activity_listeners(credential_factory: Any) -> None:
    credential, _ = credential_factory(name="Lifecycle notifier")
    with psycopg.connect(**_connection_kwargs(), autocommit=True) as listener:
        listener.execute(f"LISTEN {ACTIVITY_CHANNEL}")
        created = create_tunnel(
            credential=credential,
            idempotency_key="lifecycle-notify-create",
            root_content={"type": "text", "text": "Wake on lifecycle events."},
        )
        first = list(listener.notifies(timeout=2, stop_after=1))
        assert [item.payload for item in first] == [str(created.tunnel.id)]

        close_cycle(
            credential=credential,
            idempotency_key="lifecycle-notify-close",
            address=created.address,
            expected_cycle_id=created.cycle.id,
        )
        second = list(listener.notifies(timeout=2, stop_after=1))
        assert [item.payload for item in second] == [str(created.tunnel.id)]


@pytest.mark.asyncio
async def test_multiple_activity_listeners_receive_one_committed_event() -> None:
    tunnel_id = uuid4()
    first = ActivityListener()
    second = ActivityListener()
    first_waiter = first.register(tunnel_id)
    second_waiter = second.register(tunnel_id)
    try:
        await asyncio.wait_for(first._ready.wait(), timeout=5)
        await asyncio.wait_for(second._ready.wait(), timeout=5)
        database = await psycopg.AsyncConnection.connect(
            **_connection_kwargs(),
            autocommit=True,
        )
        async with database:
            await database.execute(
                "SELECT pg_notify(%s, %s)",
                [ACTIVITY_CHANNEL, str(tunnel_id)],
            )
        await asyncio.wait_for(first_waiter.wait(), timeout=5)
        await asyncio.wait_for(second_waiter.wait(), timeout=5)
    finally:
        first.unregister(tunnel_id, first_waiter)
        second.unregister(tunnel_id, second_waiter)
        await first.stop()
        await second.stop()


def test_request_deadline_notification_is_transactional(credential_factory: Any) -> None:
    credential, _ = credential_factory(name="Deadline notifier")
    with psycopg.connect(**_connection_kwargs(), autocommit=True) as listener:
        listener.execute(f"LISTEN {MAINTENANCE_CHANNEL}")
        with transaction.atomic():
            create_tunnel(
                credential=credential,
                idempotency_key="deadline-notify-commit",
                root_content={"type": "text", "text": "Committed deadline."},
            )
            assert list(listener.notifies(timeout=0.1, stop_after=1)) == []
        committed = list(listener.notifies(timeout=2, stop_after=1))
        assert len(committed) == 1

        with pytest.raises(RuntimeError, match="rollback"), transaction.atomic():
            create_tunnel(
                credential=credential,
                idempotency_key="deadline-notify-rollback",
                root_content={"type": "text", "text": "Rolled-back deadline."},
            )
            raise RuntimeError("rollback")
        assert list(listener.notifies(timeout=0.2, stop_after=1)) == []


def test_maintenance_lifecycle_change_does_not_notify_itself(credential_factory: Any) -> None:
    credential, _ = credential_factory(name="Maintenance notifier")
    with psycopg.connect(**_connection_kwargs(), autocommit=True) as listener:
        listener.execute(f"LISTEN {MAINTENANCE_CHANNEL}")
        created = create_tunnel(
            credential=credential,
            idempotency_key="maintenance-owned-close",
            root_content={"type": "text", "text": "Maintenance owns this close."},
        )
        assert len(list(listener.notifies(timeout=2, stop_after=1))) == 1

        now = created.cycle.expires_at + timedelta(seconds=1)
        Cycle.objects.filter(pk=created.cycle.pk).update(expires_at=now - timedelta(seconds=1))
        assert _close_expired_cycles(now=now) == (1, 1)
        assert list(listener.notifies(timeout=0.2, stop_after=1)) == []


def test_only_one_maintenance_session_can_hold_the_advisory_lock() -> None:
    with (
        psycopg.connect(**_connection_kwargs(), autocommit=True) as first,
        psycopg.connect(**_connection_kwargs(), autocommit=True) as second,
    ):
        assert first.execute("SELECT pg_try_advisory_lock(%s)", [ADVISORY_LOCK_ID]).fetchone() == (
            True,
        )
        assert second.execute("SELECT pg_try_advisory_lock(%s)", [ADVISORY_LOCK_ID]).fetchone() == (
            False,
        )
        assert first.execute("SELECT pg_advisory_unlock(%s)", [ADVISORY_LOCK_ID]).fetchone() == (
            True,
        )
        assert second.execute("SELECT pg_try_advisory_lock(%s)", [ADVISORY_LOCK_ID]).fetchone() == (
            True,
        )


def test_runtime_role_has_no_direct_history_delete_privilege() -> None:
    history_tables = (
        "tunnels_tunnel",
        "tunnels_tunneladdress",
        "tunnels_cycle",
        "tunnels_message",
        "tunnels_messagemention",
        "tunnels_tunnelevent",
        "tunnels_activitycheckpoint",
        "tunnels_auditevent",
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO startunnel"
        )
        for table in history_tables:
            cursor.execute(
                "SELECT has_table_privilege('startunnel', %s, 'DELETE')",
                [f"public.{table}"],
            )
            assert cursor.fetchone() == (True,)

    with connection.schema_editor() as schema_editor:
        revoke_runtime_history_delete(None, schema_editor)

    with connection.cursor() as cursor:
        for table in history_tables:
            for privilege in ("SELECT", "INSERT", "UPDATE"):
                cursor.execute(
                    "SELECT has_table_privilege('startunnel', %s, %s)",
                    [f"public.{table}", privilege],
                )
                assert cursor.fetchone() == (True,)
            cursor.execute(
                "SELECT has_table_privilege('startunnel', %s, 'DELETE')",
                [f"public.{table}"],
            )
            assert cursor.fetchone() == (False,)
        cursor.execute(
            "SELECT has_table_privilege('startunnel', 'public.tunnels_idempotencyrecord', 'DELETE')"
        )
        assert cursor.fetchone() == (True,)
        cursor.execute(
            "SELECT has_table_privilege(current_user, 'public.tunnels_message', 'DELETE')"
        )
        assert cursor.fetchone() == (True,)
