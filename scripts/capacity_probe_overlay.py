"""Apply a temporary, diagnostic-only overlay to an isolated StarTunnel checkout."""

# Exact source anchors keep the temporary overlay bounded.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path
from typing import TypedDict


class OverlayRecord(TypedDict):
    files: dict[str, str]
    scan_wall_seconds: float
    scan_cpu_seconds: float


def _replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"Expected one source anchor in {path}, found {count}.")
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def apply_overlay(root: Path, current_profile: Path) -> OverlayRecord:
    """Apply the short diagnostic overlay and return hashes plus scan cost."""
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    root = root.resolve()
    profile = root / "tests/load/profile.py"
    if not (root / "scripts/run_full_load_profile.py").is_file():
        raise FileNotFoundError("The isolated candidate checkout is incomplete.")

    # The safe exception-count instrumentation on current main is required here.
    shutil.copyfile(current_profile, profile)

    _replace_once(
        profile,
        "    send_tasks: set[asyncio.Task[None]] = set()\n",
        "    send_tasks: set[asyncio.Task[None]] = set()\n"
        "    capacity_rejects = 0\n"
        "    total_rejects = 0\n"
        "    rejects_with_done_tasks = 0\n"
        "    max_done_tasks = 0\n"
        "    minimum_unfinished_at_capacity_reject: int | None = None\n"
        "    capacity_rejects_below_limit = 0\n"
        "    unfinished_min: int | None = None\n"
        "    unfinished_max = 0\n"
        "    send_creates_since_sleep = 0\n"
        "    max_send_create_burst = 0\n"
        "    task_scan_wall_seconds = 0.0\n"
        "    task_scan_cpu_seconds = 0.0\n",
    )
    _replace_once(
        profile,
        "                        if delay > 0:\n"
        "                            await asyncio.sleep(delay)\n",
        "                        if delay > 0:\n"
        "                            await asyncio.sleep(delay)\n"
        "                            send_creates_since_sleep = 0\n",
    )
    _replace_once(
        profile,
        "                        if driver_observer is not None and now >= next_driver_sample:\n",
        "                        scan_wall_started = time.perf_counter()\n"
        "                        scan_cpu_started = time.process_time()\n"
        "                        done_now = sum(task.done() for task in send_tasks)\n"
        "                        unfinished_now = len(send_tasks) - done_now\n"
        "                        max_done_tasks = max(max_done_tasks, done_now)\n"
        "                        unfinished_min = (unfinished_now if unfinished_min is None else min(unfinished_min, unfinished_now))\n"
        "                        unfinished_max = max(unfinished_max, unfinished_now)\n"
        "                        task_scan_wall_seconds += time.perf_counter() - scan_wall_started\n"
        "                        task_scan_cpu_seconds += time.process_time() - scan_cpu_started\n"
        "                        if driver_observer is not None and now >= next_driver_sample:\n",
    )
    _replace_once(
        profile,
        '                                    "in_flight_sends": len(send_tasks),\n'
        '                                    "scheduled_sends": scheduled,\n',
        '                                    "in_flight_sends": len(send_tasks),\n'
        '                                    "scheduled_sends": scheduled,\n'
        '                                    "done_send_tasks": done_now,\n'
        '                                    "unfinished_send_tasks": unfinished_now,\n'
        '                                    "max_send_create_burst": max_send_create_burst,\n',
    )
    _replace_once(
        profile,
        "                        if len(send_tasks) >= MAX_IN_FLIGHT_SENDS:\n"
        '                            failures["sender_capacity"] = failures.get("sender_capacity", 0) + 1\n'
        "                            continue\n",
        "                        if len(send_tasks) >= MAX_IN_FLIGHT_SENDS:\n"
        "                            capacity_rejects += 1\n"
        "                            total_rejects += 1\n"
        "                            done_at_reject = done_now\n"
        "                            unfinished_at_reject = unfinished_now\n"
        "                            rejects_with_done_tasks += int(done_at_reject > 0)\n"
        "                            max_done_tasks = max(max_done_tasks, done_at_reject)\n"
        "                            minimum_unfinished_at_capacity_reject = (unfinished_at_reject if minimum_unfinished_at_capacity_reject is None else min(minimum_unfinished_at_capacity_reject, unfinished_at_reject))\n"
        "                            capacity_rejects_below_limit += int(unfinished_at_reject < MAX_IN_FLIGHT_SENDS)\n"
        '                            failures["sender_capacity"] = failures.get("sender_capacity", 0) + 1\n'
        "                            continue\n",
    )
    _replace_once(
        profile,
        "                        task = asyncio.create_task(send(index))\n"
        "                        scheduled += 1\n",
        "                        task = asyncio.create_task(send(index))\n"
        "                        send_creates_since_sleep += 1\n"
        "                        max_send_create_burst = max(max_send_create_burst, send_creates_since_sleep)\n"
        "                        scheduled += 1\n",
    )
    _replace_once(
        profile,
        '                            failures["missed_schedule"] = target_sends - index\n',
        '                            failures["missed_schedule"] = target_sends - index\n'
        "                            total_rejects += target_sends - index\n",
    )
    _replace_once(
        profile,
        '                    "send_latency_ms": {\n',
        '                    "capacity_diagnostic": {\n'
        '                        "capacity_rejects": capacity_rejects,\n'
        '                        "total_rejects": total_rejects,\n'
        '                        "rejects_with_done_tasks": rejects_with_done_tasks,\n'
        '                        "max_done_tasks": max_done_tasks,\n'
        '                        "minimum_unfinished_at_capacity_reject": minimum_unfinished_at_capacity_reject,\n'
        '                        "capacity_rejects_below_limit": capacity_rejects_below_limit,\n'
        '                        "unfinished_min": unfinished_min,\n'
        '                        "unfinished_max": unfinished_max,\n'
        '                        "max_send_create_burst_between_sleeps": max_send_create_burst,\n'
        '                        "task_scan_wall_seconds": task_scan_wall_seconds,\n'
        '                        "task_scan_cpu_seconds": task_scan_cpu_seconds,\n'
        "                    },\n"
        '                    "send_latency_ms": {\n',
    )

    runner = root / "scripts/run_full_load_profile.py"
    _replace_once(
        runner,
        '"STARTUNNEL_LOAD_DURATION_SECONDS": "300",',
        '"STARTUNNEL_LOAD_DURATION_SECONDS": "120",',
    )
    _replace_once(
        runner,
        '        report["release_evidence_eligible"] = (\n'
        "            passed\n"
        "            and arguments.candidate_image is not None\n"
        "            and arguments.diagnostic_lock_seconds == 0\n"
        "        )\n",
        '        report["release_evidence_eligible"] = False\n',
    )
    load_profile = root / "tests/load/run_profile.py"
    _replace_once(
        load_profile,
        '        report["release_evidence_eligible"] = (\n'
        '            bool(report["passed"]) and not arguments.diagnostic_lock_seconds\n'
        "        )\n",
        '        report["release_evidence_eligible"] = False\n',
    )

    observations = root / "scripts/load_observations.py"
    _replace_once(
        observations,
        "class PoolSnapshot(TypedDict):\n    pool: NumericFields\n",
        "class PoolSnapshot(TypedDict):\n"
        "    pool: NumericFields\n"
        "    workers: list[NumericFields]\n",
    )
    _replace_once(
        observations,
        "    errors: list[str]\n    collector: NotRequired[CollectorCounters]\n",
        "    errors: list[str]\n"
        "    collector: NotRequired[CollectorCounters]\n"
        "    host_cpu: NotRequired[dict[str, int | None]]\n"
        "    pool_workers: NotRequired[list[NumericFields]]\n",
    )
    _replace_once(
        observations,
        '    "io_waiters",\n    "oldest_wait_seconds_estimate",\n',
        '    "io_waiters",\n'
        '    "wait_lock_tuple",\n'
        '    "wait_lock_transactionid",\n'
        '    "wait_lock_relation",\n'
        '    "wait_lwlock",\n'
        '    "wait_client",\n'
        '    "blocker_count",\n'
        '    "oldest_wait_seconds_estimate",\n',
    )
    _replace_once(
        observations,
        " 'io_waiters', count(*) FILTER (WHERE wait_event_type = 'IO'),\n"
        " 'oldest_wait_seconds_estimate', coalesce(max(extract(epoch FROM clock_timestamp()-query_start))\n",
        " 'io_waiters', count(*) FILTER (WHERE wait_event_type = 'IO'),\n"
        " 'wait_lock_tuple', count(*) FILTER (WHERE wait_event_type='Lock' AND wait_event='tuple'),\n"
        " 'wait_lock_transactionid', count(*) FILTER (WHERE wait_event_type='Lock' AND wait_event='transactionid'),\n"
        " 'wait_lock_relation', count(*) FILTER (WHERE wait_event_type='Lock' AND wait_event='relation'),\n"
        " 'wait_lwlock', count(*) FILTER (WHERE wait_event_type='LWLock'),\n"
        " 'wait_client', count(*) FILTER (WHERE wait_event_type='Client'),\n"
        " 'blocker_count', (SELECT count(DISTINCT blocker_pid) FROM pg_stat_activity waiting CROSS JOIN LATERAL unnest(pg_blocking_pids(waiting.pid)) AS blockers(blocker_pid) WHERE waiting.datname=current_database() AND waiting.pid<>pg_backend_pid()),\n"
        " 'oldest_wait_seconds_estimate', coalesce(max(extract(epoch FROM clock_timestamp()-query_start))\n",
    )
    _replace_once(
        observations,
        '        sample["errors"] = errors\n',
        '        sample["errors"] = errors\n        sample["host_cpu"] = _host_cpu_counters()\n',
    )
    _replace_once(
        observations,
        "def numeric(value: object) -> int | float | None:\n",
        "def _host_cpu_counters() -> dict[str, int | None]:\n"
        '    """Read numeric host CPU counters without retaining proc headers."""\n'
        '    result: dict[str, int | None] = {"steal": None, "iowait": None, "runqueue": None}\n'
        "    try:\n"
        '        lines = Path("/proc/stat").read_text(encoding="ascii").splitlines()\n'
        '        cpu = next(line.split() for line in lines if line.startswith("cpu "))\n'
        '        result["iowait"] = int(cpu[5])\n'
        '        result["steal"] = int(cpu[8])\n'
        '        result["runqueue"] = int(lines[next(i for i, line in enumerate(lines) if line.startswith("procs_running "))].split()[1])\n'
        "    except (OSError, ValueError, IndexError, StopIteration):\n"
        "        pass\n"
        "    return result\n\n\n"
        "def numeric(value: object) -> int | float | None:\n",
    )

    pool_reader = root / "scripts/proof_pool_reader.py"
    _replace_once(
        pool_reader,
        "from prometheus_client import CollectorRegistry, multiprocess\n",
        "from prometheus_client import CollectorRegistry, multiprocess\n"
        "from pathlib import Path\n"
        "from collections.abc import Callable, Iterable\n"
        "from typing import cast\n"
        "from prometheus_client.mmap_dict import MmapedDict\n"
        "from core.json_types import JsonValue, json_value\n",
    )
    _replace_once(
        pool_reader,
        "def run(duration_seconds: float = 900) -> None:\n",
        "NumericFields = dict[str, int | float | None]\n"
        "MmapEntry = tuple[str, float, float, int]\n\n\n"
        "def read_workers() -> list[NumericFields]:\n"
        '    """Read fixed numeric pool gauges from live per-worker files."""\n'
        '    directory = os.environ.get("PROMETHEUS_MULTIPROC_DIR")\n'
        "    if not directory:\n"
        "        return []\n"
        '    fields = ("size", "available", "waiting", "queued", "errors", "requests_wait_ms")\n'
        '    statuses = ("ok", "missing", "error")\n'
        '    allowed = {f"startunnel_database_pool_{name}": name for name in fields}\n'
        "    workers: list[NumericFields] = []\n"
        "    # The pinned Prometheus client exposes this mmap reader without type annotations.\n"
        "    read_mmap_values = cast(Callable[[str], Iterable[MmapEntry]], MmapedDict.read_all_values_from_file)\n"
        '    for path in sorted(Path(directory).glob("gauge_livesum_*.db")):\n'
        '        pid_text = path.stem.removeprefix("gauge_livesum_")\n'
        "        if not pid_text.isdecimal():\n"
        "            continue\n"
        "        pid = int(pid_text)\n"
        '        worker: NumericFields = {"pid": pid, **{name: None for name in fields}, **{f"sampler_{status}": None for status in statuses}, "utime_ticks": None, "stime_ticks": None, "rss_pages": None}\n'
        "        try:\n"
        "            for key, value, _timestamp, _position in read_mmap_values(str(path)):\n"
        "                if not math.isfinite(value) or value < 0:\n"
        "                    continue\n"
        "                key_value: JsonValue = json_value(json.loads(key))\n"
        "                if not isinstance(key_value, list) or len(key_value) != 4:\n"
        "                    continue\n"
        "                metric_name, labels = key_value[0], key_value[2]\n"
        "                if not isinstance(metric_name, str) or not isinstance(labels, dict):\n"
        "                    continue\n"
        "                name = allowed.get(metric_name)\n"
        "                if name is not None and not labels:\n"
        "                    worker[name] = value\n"
        '                elif metric_name == "startunnel_database_pool_sampler_status" and len(labels) == 1:\n'
        '                    status = labels.get("status")\n'
        "                    if isinstance(status, str) and status in statuses:\n"
        '                        worker[f"sampler_{status}"] = value\n'
        "        except (OSError, ValueError, TypeError, RuntimeError):\n"
        "            continue\n"
        "        try:\n"
        '            stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()\n'
        '            worker["utime_ticks"], worker["stime_ticks"], worker["rss_pages"] = int(stat_fields[11]), int(stat_fields[12]), int(stat_fields[21])\n'
        "        except (OSError, ValueError, IndexError):\n"
        "            pass\n"
        "        workers.append(worker)\n"
        "    return workers\n\n\n"
        "def run(duration_seconds: float = 900) -> None:\n",
    )
    _replace_once(
        pool_reader,
        '                    "pool": read_pool(registry),\n',
        '                    "pool": read_pool(registry),\n'
        '                    "workers": read_workers(),\n',
    )
    _replace_once(
        observations,
        '                snapshot: PoolSnapshot = {\n                    "pool": pool,\n',
        "                workers: list[NumericFields] = []\n"
        '                raw_workers = raw.get("workers")\n'
        "                if isinstance(raw_workers, list):\n"
        "                    for worker in raw_workers[:8]:\n"
        "                        if isinstance(worker, dict):\n"
        '                            numeric_worker = numeric_fields(worker, ("pid", *POOL_FIELDS, *(f"sampler_{status}" for status in POOL_STATUSES), "utime_ticks", "stime_ticks", "rss_pages"))\n'
        '                            if numeric_worker["pid"] is not None:\n'
        "                                workers.append(numeric_worker)\n"
        "                snapshot: PoolSnapshot = {\n"
        '                    "pool": pool,\n'
        '                    "workers": workers,\n',
    )
    _replace_once(
        observations,
        '            pool = reader["pool"]\n            sample["pool"] = pool\n',
        '            pool = reader["pool"]\n'
        '            sample["pool"] = pool\n'
        '            sample["pool_workers"] = reader.get("workers", [])\n',
    )
    _replace_once(
        observations,
        "                raw = json.loads(line)\n",
        "                raw = json_object(json.loads(line))\n",
    )

    modified = (profile, runner, load_profile, observations, pool_reader)
    hashes = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in modified
    }
    return {
        "files": hashes,
        "scan_wall_seconds": time.perf_counter() - wall_start,
        "scan_cpu_seconds": time.process_time() - cpu_start,
    }
