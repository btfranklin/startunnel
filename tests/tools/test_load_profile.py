"""The fixed load workload stays paced and bounded under slow HTTP responses."""

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from tests.json_values import json_elements, json_members
from tests.load.profile import (
    Credential,
    CredentialManifest,
    LoadConfig,
    LoadProfileError,
    load_credential_manifest,
    run_load_profile,
)

from core.json_types import JsonObject, JsonValue


def test_load_manifest_accepts_readonly_symlinked_input(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "run_id": "proof-run",
                "credential_count": 1,
                "credentials": [{"credential_id": "agent-1", "key": "st_disposable"}],
            }
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o400)
    linked_manifest = tmp_path / "linked-manifest.json"
    linked_manifest.symlink_to(manifest)

    assert load_credential_manifest(linked_manifest).credentials == (
        Credential("agent-1", "st_disposable"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stall_sends,rotate_readers", [(False, False), (True, False), (False, True)]
)
async def test_load_keeps_its_deadline_and_separates_long_polls(
    monkeypatch: pytest.MonkeyPatch, stall_sends: bool, rotate_readers: bool
) -> None:
    clients: list[FakeClient] = []
    message_clients: set[int] = set()
    activity_clients: set[int] = set()
    activity_credentials: set[str] = set()

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
                if rotate_readers:
                    headers = kwargs["headers"]
                    assert isinstance(headers, dict)
                    activity_credentials.add(headers["Authorization"])
                    await asyncio.sleep(0.01)
                    return httpx.Response(200, json={"next_cursor": "cursor"})
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
        manifest=CredentialManifest(
            "fixture", tuple(Credential(str(index), f"test-key-{index}") for index in range(10))
        ),
        config=LoadConfig(
            duration_seconds=1,
            sends_per_second=20,
            concurrent_activity_readers=2 if rotate_readers else 100,
            drain_seconds=0.2,
        ),
    )
    assert time.monotonic() - started < 2
    assert len(clients) == 2
    assert message_clients and activity_clients
    assert message_clients.isdisjoint(activity_clients)
    assert isinstance(clients[1].limits, httpx.Limits)
    assert clients[1].limits.max_connections == (2 if rotate_readers else 100)
    from uvicorn import Config

    server_idle_seconds = Config("startunnel.asgi:application").timeout_keep_alive
    for connection_client in clients:
        limits = connection_client.limits
        assert isinstance(limits, httpx.Limits)
        assert limits.keepalive_expiry is not None
        assert 0 < limits.keepalive_expiry <= server_idle_seconds / 2
    if rotate_readers:
        assert activity_credentials == {f"Bearer test-key-{index}" for index in range(10)}
    assert report["passed"] is not stall_sends
    results = report["results"]
    assert isinstance(results, dict)
    if stall_sends:
        assert results["failures_by_status"] == {"send_drain_timeout": 20}
    else:
        assert results["successful_sends"] == results["expected_sends"] == 20
        assert results["failures_by_status"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "error_type"),
    [("message_send", httpx.WriteError), ("activity_poll", httpx.ReadTimeout)],
)
async def test_transport_errors_keep_operation_and_class_without_request_data(
    monkeypatch: pytest.MonkeyPatch, operation: str, error_type: type[httpx.RequestError]
) -> None:
    raised = False
    secret = "private-credential-and-payload"

    class Client:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def post(self, path: str, **kwargs: object) -> httpx.Response:
            nonlocal raised
            if path == "/api/v1/tunnels":
                return httpx.Response(
                    201,
                    json={
                        "tunnel": {"address": secret},
                        "cycle": {"id": "cycle", "root": {"id": "root"}},
                        "activity_cursor": secret,
                    },
                )
            if (
                not raised
                and path
                == {"message_send": "/api/v1/messages", "activity_poll": "/api/v1/activity"}[
                    operation
                ]
            ):
                raised = True
                raise error_type(
                    secret,
                    request=httpx.Request(
                        "POST",
                        "https://private.example/" + secret,
                        headers={"Authorization": "Bearer " + secret},
                    ),
                )
            if path == "/api/v1/activity":
                await asyncio.Event().wait()
            return httpx.Response(201 if path == "/api/v1/messages" else 204)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    report = await run_load_profile(
        base_url="http://fixture",
        host_header=None,
        manifest=CredentialManifest("run", (Credential("credential", secret),)),
        config=LoadConfig(
            duration_seconds=1, sends_per_second=1, concurrent_activity_readers=1, drain_seconds=0
        ),
    )
    assert raised
    assert report["passed"] is False
    results = report["results"]
    assert isinstance(results, dict)
    assert results["failures_by_status"] == {"transport": 1}
    assert results["transport_errors_by_operation"] == {
        "message_send": {error_type.__name__: 1} if operation == "message_send" else {},
        "activity_poll": {error_type.__name__: 1} if operation == "activity_poll" else {},
    }
    assert secret not in json.dumps(report)
    assert "private.example" not in json.dumps(report)


def test_custom_error_names_do_not_enter_the_safe_report() -> None:
    from tests.load.profile import _http_exception_type

    custom_error = type("private_credential", (httpx.ReadTimeout,), {})
    assert _http_exception_type(custom_error("private request data")) == "ReadTimeout"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", 42),
        ("credential_id", None),
        ("key", ["private-key"]),
        ("credentials", {"key": "private-key"}),
        ("credential_count", "1"),
        ("credential_count", True),
    ],
)
def test_load_manifest_rejects_wrong_identity_field_types(
    tmp_path: Path, field: str, value: JsonValue
) -> None:
    document: JsonObject = {
        "run_id": "private-run",
        "credential_count": 1,
        "credentials": [{"credential_id": "private-agent", "key": "private-key"}],
    }
    if field in {"credential_id", "key"}:
        credential = json_members(json_elements(document["credentials"])[0])
        credential[field] = value
    else:
        document[field] = value
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(LoadProfileError) as caught:
        load_credential_manifest(path)

    assert "private" not in str(caught.value)


def _minimal_created_tunnel() -> JsonObject:
    return {
        "tunnel": {"address": "private-address"},
        "cycle": {"id": "private-cycle", "root": {"id": "private-root"}},
        "activity_cursor": "private-cursor",
    }


@pytest.mark.asyncio
async def test_reader_task_failure_stops_the_proof_without_private_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def post(self, path: str, **kwargs: object) -> httpx.Response:
            if path == "/api/v1/tunnels":
                return httpx.Response(201, json=_minimal_created_tunnel())
            if path == "/api/v1/activity":
                raise RuntimeError("private-transport-details")
            return httpx.Response(201)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    initial = set(asyncio.all_tasks())
    report: dict[str, object] = {}

    with pytest.raises(LoadProfileError, match="The activity readers failed") as caught:
        await run_load_profile(
            base_url="http://fixture",
            host_header=None,
            manifest=CredentialManifest("run", (Credential("id", "private-key"),)),
            config=LoadConfig(
                duration_seconds=1, sends_per_second=1, concurrent_activity_readers=1
            ),
            partial_report=report,
        )

    assert report["passed"] is False
    assert "private" not in str(caught.value)
    assert "private" not in json.dumps(report)
    assert set(asyncio.all_tasks()) == initial


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tunnel", []),
        ("cycle", None),
        ("root", "private-payload"),
        ("address", {"payload": "private-payload"}),
        ("cycle_id", 42),
        ("root_id", False),
        ("activity_cursor", ["private-cursor"]),
    ],
)
async def test_malformed_created_identity_fails_with_a_safe_report(
    monkeypatch: pytest.MonkeyPatch, field: str, value: JsonValue
) -> None:
    body = _minimal_created_tunnel()
    if field == "root":
        json_members(body["cycle"])["root"] = value
    elif field == "address":
        json_members(body["tunnel"])["address"] = value
    elif field == "cycle_id":
        json_members(body["cycle"])["id"] = value
    elif field == "root_id":
        json_members(json_members(body["cycle"])["root"])["id"] = value
    else:
        body[field] = value

    class Client:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def post(self, path: str, **kwargs: object) -> httpx.Response:
            assert path == "/api/v1/tunnels"
            return httpx.Response(201, json=body)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    report: dict[str, object] = {}
    with pytest.raises(LoadProfileError) as caught:
        await run_load_profile(
            base_url="http://fixture",
            host_header=None,
            manifest=CredentialManifest("private-run", (Credential("private-id", "private-key"),)),
            config=LoadConfig(
                duration_seconds=1, sends_per_second=1, concurrent_activity_readers=1
            ),
            partial_report=report,
        )

    assert report["passed"] is False
    results = report["results"]
    assert isinstance(results, dict)
    assert results["failures_by_status"] == {"invalid_response": 1}
    assert results["successful_sends"] == 0
    assert "private" not in str(caught.value)
    assert "private" not in json.dumps(report)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [None, [], {}, {"next_cursor": 42}, {"next_cursor": {"payload": "private-payload"}}]
)
async def test_malformed_activity_cursor_fails_with_a_safe_report(
    monkeypatch: pytest.MonkeyPatch, body: JsonValue
) -> None:
    class Client:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def post(self, path: str, **kwargs: object) -> httpx.Response:
            if path == "/api/v1/tunnels":
                return httpx.Response(201, json=_minimal_created_tunnel())
            if path == "/api/v1/activity":
                return httpx.Response(200, json=body)
            return httpx.Response(201 if path == "/api/v1/messages" else 204)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    report = await run_load_profile(
        base_url="http://fixture",
        host_header=None,
        manifest=CredentialManifest("private-run", (Credential("private-id", "private-key"),)),
        config=LoadConfig(
            duration_seconds=1,
            sends_per_second=1,
            concurrent_activity_readers=1,
            drain_seconds=0,
        ),
    )

    assert report["passed"] is False
    results = report["results"]
    assert isinstance(results, dict)
    assert results["failures_by_status"] == {"invalid_response": 1}
    assert results["successful_sends"] == 1
    assert results["activity_reads"] == 0
    assert "private" not in json.dumps(report)
