"""Provider-neutral HTTP load proof for one StarTunnel instance."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict
from uuid import uuid4

import httpx
from scripts.load_diagnostic import DiagnosticLock
from scripts.proof_timing import PhaseTimings

from core.json_types import JsonValue, json_object, json_value

MAX_IN_FLIGHT_SENDS = 256
# Expire idle client connections before the server's five-second idle close.
CLIENT_KEEPALIVE_SECONDS = 1.0


class LoadProfileError(RuntimeError):
    """The load proof configuration or result is invalid."""


@dataclass(frozen=True, slots=True)
class Credential:
    credential_id: str
    key: str


@dataclass(frozen=True, slots=True)
class CredentialManifest:
    run_id: str
    credentials: tuple[Credential, ...]


@dataclass(frozen=True, slots=True)
class LoadConfig:
    duration_seconds: float = 300.0
    sends_per_second: float = 50.0
    concurrent_activity_readers: int = 100
    drain_seconds: float = 30.0

    def validate(self) -> None:
        if not 1 <= self.duration_seconds <= 3600:
            raise ValueError("duration_seconds must be from 1 through 3600.")
        if not 0.1 <= self.sends_per_second <= 500:
            raise ValueError("sends_per_second must be from 0.1 through 500.")
        if not 0 <= self.concurrent_activity_readers <= 1000:
            raise ValueError("concurrent_activity_readers must be from 0 through 1000.")
        if not 0 <= self.drain_seconds <= 300:
            raise ValueError("drain_seconds must be from 0 through 300.")


@dataclass(frozen=True, slots=True)
class _LoadTunnel:
    address: str
    cycle_id: str
    root_id: str
    cursor: str
    creator_key: str


class _TextContent(TypedDict):
    type: Literal["text"]
    text: str


class _RootRequest(TypedDict):
    content: _TextContent


class _CycleRequest(TypedDict):
    label: str
    expires_in_seconds: int
    root: _RootRequest


class _CreateRequest(TypedDict):
    label: str
    cycle: _CycleRequest


class _ActivityRequest(TypedDict):
    address: str
    after_cursor: str
    wait_seconds: int
    limit: int


class _ReplyRequest(TypedDict):
    address: str
    parent_id: str
    content: _TextContent


class _CloseRequest(TypedDict):
    address: str
    expected_cycle_id: str


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("The load field must be text.")
    return value


def _array(value: object) -> list[JsonValue]:
    checked = json_value(value)
    if not isinstance(checked, list):
        raise ValueError("The load manifest credentials must be an array.")
    return checked


def _created_tunnel(value: object, *, creator_key: str) -> _LoadTunnel:
    body = json_object(value)
    tunnel = json_object(body["tunnel"])
    cycle = json_object(body["cycle"])
    root = json_object(cycle["root"])
    return _LoadTunnel(
        address=_text(tunnel["address"]),
        cycle_id=_text(cycle["id"]),
        root_id=_text(root["id"]),
        cursor=_text(body["activity_cursor"]),
        creator_key=creator_key,
    )


def _activity_cursor(value: object) -> str:
    return _text(json_object(value)["next_cursor"])


def default_manifest_path() -> Path:
    return Path("/tmp") / f"startunnel-load-{uuid4().hex}.json"


def load_credential_manifest(path: Path) -> CredentialManifest:
    if not path.is_file():
        raise LoadProfileError("The load credential manifest is not a regular file.")
    try:
        decoded: object = json.loads(path.read_text(encoding="utf-8"))
        document = json_object(decoded)
        entries = _array(document["credentials"])
        credentials = tuple(
            Credential(
                credential_id=_text(json_object(item)["credential_id"]),
                key=_text(json_object(item)["key"]),
            )
            for item in entries
        )
        manifest = CredentialManifest(
            run_id=_text(document["run_id"]),
            credentials=credentials,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise LoadProfileError("The load credential manifest is not valid.") from error
    credential_count = document.get("credential_count", -1)
    if (
        not isinstance(credential_count, int)
        or isinstance(credential_count, bool)
        or len(credentials) != credential_count
        or not credentials
    ):
        raise LoadProfileError("The load credential count is not valid.")
    return manifest


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return round(ordered[index], 3)


def _http_exception_type(error: httpx.HTTPError) -> str:
    """Return a public HTTPX class name without exception or request data."""
    for error_class in type(error).__mro__:
        name: str = error_class.__name__
        exported: object = getattr(httpx, name, None)
        if issubclass(error_class, httpx.HTTPError) and exported is error_class:
            return name
    return "HTTPError"


async def run_load_profile(
    *,
    base_url: str,
    host_header: str | None,
    manifest: CredentialManifest,
    config: LoadConfig,
    diagnostic_lock_seconds: int = 0,
    phases: PhaseTimings | None = None,
    partial_report: dict[str, object] | None = None,
    driver_observer: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    config.validate()
    phases = phases or PhaseTimings()
    report = partial_report if partial_report is not None else {}
    scheduled = 0
    max_in_flight = 0
    max_scheduling_lag = 0.0
    total_scheduling_lag = 0.0
    scheduling_observations = 0
    workload_completed = False
    common_headers = {"Host": host_header} if host_header else {}
    timings: list[float] = []
    failures: dict[str, int] = {}
    transport_errors: dict[Literal["message_send", "activity_poll"], dict[str, int]] = {
        "message_send": {},
        "activity_poll": {},
    }

    def record_transport_error(
        operation: Literal["message_send", "activity_poll"], error: httpx.HTTPError
    ) -> None:
        failures["transport"] = failures.get("transport", 0) + 1
        counts = transport_errors[operation]
        name = _http_exception_type(error)
        counts[name] = counts.get(name, 0) + 1

    sent = 0
    reads = 0
    stopped = asyncio.Event()
    reader_group_started = asyncio.Event()
    tunnel_count = min(10, max(1, len(manifest.credentials) // 3))
    tunnels: list[_LoadTunnel] = []
    reader_supervisor: asyncio.Task[None] | None = None
    send_tasks: set[asyncio.Task[None]] = set()

    try:
        # Long polls must not occupy the connections used to submit messages.
        async with (
            httpx.AsyncClient(
                base_url=base_url,
                timeout=30,
                headers=common_headers,
                limits=httpx.Limits(
                    max_connections=MAX_IN_FLIGHT_SENDS,
                    max_keepalive_connections=MAX_IN_FLIGHT_SENDS,
                    keepalive_expiry=CLIENT_KEEPALIVE_SECONDS,
                ),
            ) as client,
            httpx.AsyncClient(
                base_url=base_url,
                timeout=30,
                headers=common_headers,
                limits=httpx.Limits(
                    max_connections=max(1, config.concurrent_activity_readers),
                    keepalive_expiry=CLIENT_KEEPALIVE_SECONDS,
                ),
            ) as activity_client,
        ):
            with phases.phase("tunnel_setup"):
                for index in range(tunnel_count):
                    credential = manifest.credentials[index]
                    create_request: _CreateRequest = {
                        "label": f"Load tunnel {index}",
                        "cycle": {
                            "label": "Load cycle",
                            "expires_in_seconds": 3600,
                            "root": {"content": {"type": "text", "text": "Load root."}},
                        },
                    }
                    response = await client.post(
                        "/api/v1/tunnels",
                        headers={
                            "Authorization": f"Bearer {credential.key}",
                            "Idempotency-Key": f"load-create-{uuid4().hex}",
                        },
                        json=create_request,
                    )
                    if response.status_code != 201:
                        raise LoadProfileError("The load proof could not create its tunnels.")
                    try:
                        created_value: object = response.json()
                        tunnel = _created_tunnel(created_value, creator_key=credential.key)
                    except (KeyError, TypeError, ValueError) as error:
                        failures["invalid_response"] = failures.get("invalid_response", 0) + 1
                        raise LoadProfileError("The load tunnel response is not valid.") from error
                    tunnels.append(tunnel)
            with DiagnosticLock(diagnostic_lock_seconds, Path(__file__).resolve().parents[2]):

                async def reader(reader_index: int) -> None:
                    nonlocal reads
                    tunnel = tunnels[reader_index % len(tunnels)]
                    cursor = tunnel.cursor
                    request_index = reader_index
                    while not stopped.is_set():
                        credential = manifest.credentials[request_index % len(manifest.credentials)]
                        request_index += config.concurrent_activity_readers
                        try:
                            activity_request: _ActivityRequest = {
                                "address": tunnel.address,
                                "after_cursor": cursor,
                                "wait_seconds": 20,
                                "limit": 100,
                            }
                            response = await activity_client.post(
                                "/api/v1/activity",
                                headers={"Authorization": f"Bearer {credential.key}"},
                                json=activity_request,
                            )
                            if response.status_code == 200:
                                try:
                                    activity_value: object = response.json()
                                    cursor = _activity_cursor(activity_value)
                                except KeyError, TypeError, ValueError:
                                    failures["invalid_response"] = (
                                        failures.get("invalid_response", 0) + 1
                                    )
                                    return
                                reads += 1
                            else:
                                status = str(response.status_code)
                                failures[status] = failures.get(status, 0) + 1
                        except httpx.HTTPError as error:
                            record_transport_error("activity_poll", error)

                async def supervise_readers() -> None:
                    try:
                        async with asyncio.TaskGroup() as group:
                            for index in range(config.concurrent_activity_readers):
                                group.create_task(reader(index))
                            reader_group_started.set()
                            await stopped.wait()
                            group.cancel()
                    except ExceptionGroup:
                        raise LoadProfileError("The activity readers failed.") from None

                reader_supervisor = asyncio.create_task(supervise_readers())
                await reader_group_started.wait()
                started = time.monotonic()
                target_sends = int(config.duration_seconds * config.sends_per_second)
                deadline = started + config.duration_seconds
                send_tasks = set()

                async def send(index: int) -> None:
                    nonlocal sent
                    tunnel = tunnels[index % len(tunnels)]
                    credential = manifest.credentials[
                        (index + tunnel_count) % len(manifest.credentials)
                    ]
                    request_started = time.monotonic()
                    try:
                        reply_request: _ReplyRequest = {
                            "address": tunnel.address,
                            "parent_id": tunnel.root_id,
                            "content": {"type": "text", "text": "Load reply."},
                        }
                        response = await client.post(
                            "/api/v1/messages",
                            headers={
                                "Authorization": f"Bearer {credential.key}",
                                "Idempotency-Key": f"load-reply-{uuid4().hex}",
                            },
                            json=reply_request,
                        )
                        timings.append((time.monotonic() - request_started) * 1000)
                        if response.status_code == 201:
                            sent += 1
                        else:
                            status = str(response.status_code)
                            failures[status] = failures.get(status, 0) + 1
                    except httpx.HTTPError as error:
                        record_transport_error("message_send", error)

                next_driver_sample = started
                with phases.phase("traffic"):
                    for index in range(target_sends):
                        delay = started + index / config.sends_per_second - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                        now = time.monotonic()
                        lag = max(0.0, now - (started + index / config.sends_per_second))
                        max_scheduling_lag = max(max_scheduling_lag, lag)
                        total_scheduling_lag += lag
                        scheduling_observations += 1
                        if driver_observer is not None and now >= next_driver_sample:
                            driver_observer(
                                {
                                    "captured_at": datetime.now(UTC).isoformat(),
                                    "monotonic_seconds": now,
                                    "scheduling_lag_seconds": lag,
                                    "due_unscheduled": max(
                                        0,
                                        min(
                                            target_sends,
                                            int((now - started) * config.sends_per_second) + 1,
                                        )
                                        - index,
                                    ),
                                    "in_flight_sends": len(send_tasks),
                                    "scheduled_sends": scheduled,
                                }
                            )
                            next_driver_sample = now + 1
                        if time.monotonic() >= deadline:
                            failures["missed_schedule"] = target_sends - index
                            break
                        if len(send_tasks) >= MAX_IN_FLIGHT_SENDS:
                            failures["sender_capacity"] = failures.get("sender_capacity", 0) + 1
                            continue
                        task = asyncio.create_task(send(index))
                        scheduled += 1
                        send_tasks.add(task)
                        max_in_flight = max(max_in_flight, len(send_tasks))
                        task.add_done_callback(send_tasks.discard)
                        if index and index % max(1, int(config.sends_per_second * 60)) == 0:
                            print(
                                f"Load progress: {index} scheduled, {sent} successful sends.",
                                flush=True,
                            )

                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                with phases.phase("drain"):
                    stopped.set()
                    if reader_supervisor is not None:
                        await reader_supervisor
                    outstanding = list(send_tasks)
                    if outstanding:
                        done, pending = await asyncio.wait(
                            outstanding, timeout=config.drain_seconds
                        )
                        unfinished_sends = sum(task in send_tasks for task in pending)
                        if unfinished_sends:
                            failures["send_drain_timeout"] = unfinished_sends
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*done, *pending, return_exceptions=True)
                with phases.phase("cycle_close_verification"):
                    for tunnel in tunnels:
                        close_request: _CloseRequest = {
                            "address": tunnel.address,
                            "expected_cycle_id": tunnel.cycle_id,
                        }
                        response = await client.post(
                            "/api/v1/cycles/close",
                            headers={
                                "Authorization": f"Bearer {tunnel.creator_key}",
                                "Idempotency-Key": f"load-close-{uuid4().hex}",
                            },
                            json=close_request,
                        )
                        if response.status_code != 204:
                            failures[str(response.status_code)] = (
                                failures.get(str(response.status_code), 0) + 1
                            )
        workload_completed = True

    finally:
        stopped.set()
        if reader_supervisor is not None:
            await asyncio.gather(reader_supervisor, return_exceptions=True)
        for task in send_tasks:
            if not task.done():
                task.cancel()
        if send_tasks:
            await asyncio.gather(*send_tasks, return_exceptions=True)
        expected = int(config.duration_seconds * config.sends_per_second)
        passed = workload_completed and not failures and sent >= max(1, int(expected * 0.95))
        report.update(
            {
                "passed": passed,
                "configuration": {
                    "duration_seconds": config.duration_seconds,
                    "drain_seconds": config.drain_seconds,
                    "sends_per_second": config.sends_per_second,
                    "concurrent_activity_readers": config.concurrent_activity_readers,
                    "max_in_flight_sends": MAX_IN_FLIGHT_SENDS,
                    "web_workers": os.getenv("STARTUNNEL_LOAD_WEB_WORKERS", "unknown"),
                    "web_database_pool_max_size": os.getenv(
                        "STARTUNNEL_LOAD_WEB_DATABASE_POOL_MAX_SIZE", "unknown"
                    ),
                    "credential_count": len(manifest.credentials),
                    "tunnel_count": tunnel_count,
                },
                "results": {
                    "expected_sends": expected,
                    "successful_sends": sent,
                    "activity_reads": reads,
                    "failures_by_status": failures,
                    "transport_errors_by_operation": transport_errors,
                    "driver": {
                        "scheduled_sends": scheduled,
                        "unscheduled_sends": expected - scheduled,
                        "max_in_flight_sends": max_in_flight,
                        "max_scheduling_lag_seconds": max_scheduling_lag,
                        "mean_scheduling_lag_seconds": total_scheduling_lag
                        / max(1, scheduling_observations),
                    },
                    "send_latency_ms": {
                        "p50": _percentile(timings, 0.50),
                        "p95": _percentile(timings, 0.95),
                        "p99": _percentile(timings, 0.99),
                    },
                },
                "provenance": {
                    "build_mode": os.getenv("STARTUNNEL_LOAD_BUILD_MODE", "unknown"),
                    "docker_version": os.getenv("STARTUNNEL_LOAD_DOCKER_VERSION", "unknown"),
                    "image_ids": os.getenv("STARTUNNEL_LOAD_IMAGE_DIGESTS", "unknown"),
                },
                "fixture_cleanup_complete": False,
            }
        )
    return report


def finalize_fixture_cleanup(report: dict[str, object], *, complete: bool) -> None:
    report["fixture_cleanup_complete"] = complete
    if not complete:
        report["passed"] = False


def write_safe_report(path: Path, report: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise OSError("The report path already exists.")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(report, output, indent=2, sort_keys=True)
            output.write("\n")
    finally:
        with suppress(OSError):
            os.close(descriptor)
