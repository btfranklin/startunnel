"""The fixed load workload stays paced and bounded under slow HTTP responses."""

import asyncio
import time

import httpx
import pytest
from tests.load.profile import Credential, CredentialManifest, LoadConfig, run_load_profile


@pytest.mark.asyncio
@pytest.mark.parametrize("stall_sends", [False, True])
async def test_load_keeps_its_deadline_and_separates_long_polls(
    monkeypatch: pytest.MonkeyPatch, stall_sends: bool
) -> None:
    clients: list[FakeClient] = []
    message_clients: set[int] = set()
    activity_clients: set[int] = set()

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            self.limits = kwargs.get("limits")
            clients.append(self)

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def post(self, path: str, **kwargs: object) -> httpx.Response:
            if path == "/api/v1/tunnels":
                return httpx.Response(
                    201,
                    json={
                        "tunnel": {"address": "fixture"},
                        "cycle": {"id": "cycle", "root": {"id": "root"}},
                        "activity_cursor": "cursor",
                    },
                )
            if path == "/api/v1/activity":
                activity_clients.add(id(self))
                await asyncio.Event().wait()
            if path == "/api/v1/messages":
                message_clients.add(id(self))
                if stall_sends:
                    await asyncio.Event().wait()
                # Slower than the 50 ms send interval: serial submission cannot
                # deliver the required workload inside its one-second window.
                await asyncio.sleep(0.08)
                return httpx.Response(201)
            return httpx.Response(204)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    started = time.monotonic()
    report = await run_load_profile(
        base_url="http://fixture",
        host_header=None,
        metrics_token=None,
        manifest=CredentialManifest("fixture", 1, (Credential("credential", "test-key"),)),
        config=LoadConfig(
            duration_seconds=1,
            sends_per_second=20,
            concurrent_activity_readers=100,
            drain_seconds=0.2,
        ),
    )
    assert time.monotonic() - started < 2
    assert len(clients) == 2
    assert message_clients and activity_clients
    assert message_clients.isdisjoint(activity_clients)
    assert isinstance(clients[1].limits, httpx.Limits)
    assert clients[1].limits.max_connections == 100
    assert report["passed"] is not stall_sends
    results = report["results"]
    assert isinstance(results, dict)
    if stall_sends:
        assert results["failures_by_status"] == {"send_drain_timeout": 20}
    else:
        assert results["successful_sends"] == results["expected_sends"] == 20
        assert results["failures_by_status"] == {}
