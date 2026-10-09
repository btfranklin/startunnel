"""Provider-neutral HTTP load proof for one StarTunnel instance."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx


class LoadProfileError(RuntimeError):
    """The load proof configuration or result is invalid."""


@dataclass(frozen=True, slots=True)
class Credential:
    credential_id: str
    key: str


@dataclass(frozen=True, slots=True)
class CredentialManifest:
    run_id: str
    user_count: int
    credentials: tuple[Credential, ...]


@dataclass(frozen=True, slots=True)
class LoadConfig:
    duration_seconds: float = 300.0
    sends_per_second: float = 50.0
    concurrent_activity_readers: int = 100
    drain_seconds: float = 30.0
    require_cleanup_lag: bool = True
    seed: int = 20260810

    def validate(self) -> None:
        if not 1 <= self.duration_seconds <= 3600:
            raise ValueError("duration_seconds must be from 1 through 3600.")
        if not 0.1 <= self.sends_per_second <= 500:
            raise ValueError("sends_per_second must be from 0.1 through 500.")
        if not 0 <= self.concurrent_activity_readers <= 1000:
            raise ValueError("concurrent_activity_readers must be from 0 through 1000.")
        if not 0 <= self.drain_seconds <= 300:
            raise ValueError("drain_seconds must be from 0 through 300.")


def default_manifest_path() -> Path:
    return Path("/tmp") / f"startunnel-load-{uuid4().hex}.json"


def load_credential_manifest(path: Path) -> CredentialManifest:
    if path.is_symlink() or not path.is_file():
        raise LoadProfileError("The load credential manifest is not a regular file.")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise LoadProfileError("The load credential manifest must have mode 0600.")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        entries = document["credentials"]
        credentials = tuple(
            Credential(credential_id=str(item["credential_id"]), key=str(item["key"]))
            for item in entries
        )
        manifest = CredentialManifest(
            run_id=str(document["run_id"]),
            user_count=int(document["user_count"]),
            credentials=credentials,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise LoadProfileError("The load credential manifest is not valid.") from error
    if len(credentials) != int(document.get("credential_count", -1)) or not credentials:
        raise LoadProfileError("The load credential count is not valid.")
    return manifest


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return round(ordered[index], 3)


async def run_load_profile(
    *,
    base_url: str,
    host_header: str | None,
    metrics_token: str | None,
    manifest: CredentialManifest,
    config: LoadConfig,
) -> dict[str, object]:
    del metrics_token
    config.validate()
    common_headers = {"Host": host_header} if host_header else {}
    timings: list[float] = []
    failures: dict[str, int] = {}
    sent = 0
    reads = 0
    stopped = asyncio.Event()
    tunnel_count = min(10, max(1, len(manifest.credentials) // 3))
    tunnels: list[dict[str, str]] = []

    # Long polls must not occupy the connections used to submit messages.
    async with (
        httpx.AsyncClient(base_url=base_url, timeout=30, headers=common_headers) as client,
        httpx.AsyncClient(
            base_url=base_url,
            timeout=30,
            headers=common_headers,
            limits=httpx.Limits(max_connections=max(1, config.concurrent_activity_readers)),
        ) as activity_client,
    ):
        for index in range(tunnel_count):
            credential = manifest.credentials[index]
            response = await client.post(
                "/api/v1/tunnels",
                headers={
                    "Authorization": f"Bearer {credential.key}",
                    "Idempotency-Key": f"load-create-{uuid4().hex}",
                },
                json={
                    "label": f"Load tunnel {index}",
                    "cycle": {
                        "label": "Load cycle",
                        "expires_in_seconds": 3600,
                        "root": {"content": {"type": "text", "text": "Load root."}},
                    },
                },
            )
            if response.status_code != 201:
                raise LoadProfileError("The load proof could not create its tunnels.")
            body = response.json()
            tunnels.append(
                {
                    "address": str(body["tunnel"]["address"]),
                    "cycle_id": str(body["cycle"]["id"]),
                    "root_id": str(body["cycle"]["root"]["id"]),
                    "cursor": str(body["activity_cursor"]),
                    "creator_key": credential.key,
                }
            )

        async def reader(reader_index: int) -> None:
            nonlocal reads
            tunnel = tunnels[reader_index % len(tunnels)]
            credential = manifest.credentials[reader_index % len(manifest.credentials)]
            cursor = tunnel["cursor"]
            while not stopped.is_set():
                try:
                    response = await activity_client.post(
                        "/api/v1/activity",
                        headers={"Authorization": f"Bearer {credential.key}"},
                        json={
                            "address": tunnel["address"],
                            "after_cursor": cursor,
                            "wait_seconds": 20,
                            "limit": 100,
                        },
                    )
                    if response.status_code == 200:
                        cursor = str(response.json()["next_cursor"])
                        reads += 1
                    else:
                        status = str(response.status_code)
                        failures[status] = failures.get(status, 0) + 1
                except httpx.HTTPError:
                    failures["transport"] = failures.get("transport", 0) + 1

        readers = [
            asyncio.create_task(reader(index))
            for index in range(config.concurrent_activity_readers)
        ]
        started = time.monotonic()
        target_sends = int(config.duration_seconds * config.sends_per_second)
        deadline = started + config.duration_seconds
        send_tasks: set[asyncio.Task[None]] = set()

        async def send(index: int) -> None:
            nonlocal sent
            tunnel = tunnels[index % len(tunnels)]
            credential = manifest.credentials[(index + tunnel_count) % len(manifest.credentials)]
            request_started = time.monotonic()
            try:
                response = await client.post(
                    "/api/v1/messages",
                    headers={
                        "Authorization": f"Bearer {credential.key}",
                        "Idempotency-Key": f"load-reply-{uuid4().hex}",
                    },
                    json={
                        "address": tunnel["address"],
                        "parent_id": tunnel["root_id"],
                        "content": {"type": "text", "text": "Load reply."},
                    },
                )
                timings.append((time.monotonic() - request_started) * 1000)
                if response.status_code == 201:
                    sent += 1
                else:
                    status = str(response.status_code)
                    failures[status] = failures.get(status, 0) + 1
            except httpx.HTTPError:
                failures["transport"] = failures.get("transport", 0) + 1

        for index in range(target_sends):
            delay = started + index / config.sends_per_second - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if time.monotonic() >= deadline:
                failures["missed_schedule"] = target_sends - index
                break
            if len(send_tasks) >= 100:
                failures["sender_capacity"] = failures.get("sender_capacity", 0) + 1
                continue
            task = asyncio.create_task(send(index))
            send_tasks.add(task)
            task.add_done_callback(send_tasks.discard)
            if index and index % max(1, int(config.sends_per_second * 60)) == 0:
                print(f"Load progress: {index} scheduled, {sent} successful sends.", flush=True)

        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

        stopped.set()
        outstanding = [*readers, *send_tasks]
        if outstanding:
            done, pending = await asyncio.wait(outstanding, timeout=config.drain_seconds)
            unfinished_sends = sum(task in send_tasks for task in pending)
            if unfinished_sends:
                failures["send_drain_timeout"] = unfinished_sends
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)

        for tunnel in tunnels:
            response = await client.post(
                "/api/v1/cycles/close",
                headers={
                    "Authorization": f"Bearer {tunnel['creator_key']}",
                    "Idempotency-Key": f"load-close-{uuid4().hex}",
                },
                json={
                    "address": tunnel["address"],
                    "expected_cycle_id": tunnel["cycle_id"],
                },
            )
            if response.status_code != 204:
                failures[str(response.status_code)] = failures.get(str(response.status_code), 0) + 1

    expected = int(config.duration_seconds * config.sends_per_second)
    passed = not failures and sent >= max(1, int(expected * 0.95))
    return {
        "passed": passed,
        "configuration": {
            "duration_seconds": config.duration_seconds,
            "sends_per_second": config.sends_per_second,
            "concurrent_activity_readers": config.concurrent_activity_readers,
            "credential_count": len(manifest.credentials),
            "tunnel_count": tunnel_count,
            "seed": config.seed,
        },
        "results": {
            "expected_sends": expected,
            "successful_sends": sent,
            "activity_reads": reads,
            "failures_by_status": failures,
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
