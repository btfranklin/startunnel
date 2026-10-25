"""Compare UUIDv4 and UUIDv7 generation and PostgreSQL insert costs."""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4, uuid7

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row

DEFAULT_ROWS = 100_000
DEFAULT_TRIALS = 5
MAX_ROWS = 100_000
MAX_TRIALS = 9
PAYLOAD = "x" * 256
STATEMENT_TIMEOUT_MS = 60_000

type Generator = Callable[[], UUID]
type VersionName = Literal["uuid4", "uuid7"]


@dataclass(frozen=True, slots=True)
class _TrialResult:
    generation_ms: float
    insert_ms: float
    index_bytes: int


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    return parser


def _validate_environment(environ: Mapping[str, str]) -> str:
    if (
        environ.get("STARTUNNEL_ENV") != "test"
        or environ.get("DJANGO_SETTINGS_MODULE") != "startunnel.settings.test"
    ):
        raise RuntimeError("The UUID benchmark requires the StarTunnel test environment.")
    database_url = environ.get("TEST_DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("The UUID benchmark requires a test PostgreSQL database.")
    scheme = database_url.partition(":")[0].casefold()
    if scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("The UUID benchmark requires PostgreSQL.")

    import django
    from django.conf import settings

    django.setup()
    database = settings.DATABASES.get("default", {})
    if (
        settings.STARTUNNEL_ENV != "test"
        or database.get("ENGINE") != "django.db.backends.postgresql"
    ):
        raise RuntimeError("The UUID benchmark requires PostgreSQL test settings.")
    return database_url


def _validate_options(rows: int, trials: int) -> None:
    if not 1 <= rows <= MAX_ROWS:
        raise ValueError(f"rows must be from 1 through {MAX_ROWS}.")
    if not 1 <= trials <= MAX_TRIALS:
        raise ValueError(f"trials must be from 1 through {MAX_TRIALS}.")


def _run_trial(
    connection: psycopg.Connection[tuple[object, ...]],
    *,
    generator: Generator,
    version_name: VersionName,
    trial_index: int,
    row_count: int,
) -> _TrialResult:
    generation_started = time.perf_counter_ns()
    identifiers = [generator() for _ in range(row_count)]
    generation_ms = (time.perf_counter_ns() - generation_started) / 1_000_000

    cycle_id = uuid4()
    rows = [
        (identifier, cycle_id, sequence, PAYLOAD) for sequence, identifier in enumerate(identifiers)
    ]
    table_name = f"uuid_bench_{version_name}_{trial_index}"

    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
        cursor.execute(
            sql.SQL(
                "CREATE TEMP TABLE {} ("
                "id uuid PRIMARY KEY, "
                "cycle_id uuid NOT NULL, "
                "sequence bigint NOT NULL, "
                "payload text NOT NULL, "
                "UNIQUE (cycle_id, sequence)"
                ") ON COMMIT DROP"
            ).format(sql.Identifier(table_name))
        )
        insert_statement = sql.SQL("COPY {} (id, cycle_id, sequence, payload) FROM STDIN").format(
            sql.Identifier(table_name)
        )
        insert_started = time.perf_counter_ns()
        with cursor.copy(insert_statement) as copy:
            for row in rows:
                copy.write_row(row)
        insert_ms = (time.perf_counter_ns() - insert_started) / 1_000_000
        cursor.execute(
            "SELECT COALESCE(sum(pg_relation_size(indexrelid)), 0)::bigint "
            "FROM pg_index WHERE indrelid = %s::regclass",
            (table_name,),
        )
        index_row = cursor.fetchone()
        if index_row is None or len(index_row) != 1 or not isinstance(index_row[0], int):
            raise RuntimeError("PostgreSQL did not return the temporary index size.")
        index_bytes = index_row[0]

    return _TrialResult(
        generation_ms=round(generation_ms, 3),
        insert_ms=round(insert_ms, 3),
        index_bytes=int(index_bytes),
    )


def _version_results(trials: list[_TrialResult]) -> dict[str, object]:
    generation_times = [trial.generation_ms for trial in trials]
    insert_times = [trial.insert_ms for trial in trials]
    index_sizes = [trial.index_bytes for trial in trials]
    return {
        "generation_ms": generation_times,
        "insert_ms": insert_times,
        "btree_index_bytes": index_sizes,
        "median_generation_ms": round(statistics.median(generation_times), 3),
        "median_insert_ms": round(statistics.median(insert_times), 3),
        "median_btree_index_bytes": int(statistics.median(index_sizes)),
    }


def benchmark(database_url: str, *, rows: int, trials: int) -> dict[str, object]:
    _validate_options(rows, trials)
    generators: dict[VersionName, Generator] = {
        "uuid4": uuid4,
        "uuid7": uuid7,
    }
    results: dict[VersionName, list[_TrialResult]] = {"uuid4": [], "uuid7": []}
    with psycopg.connect(
        database_url,
        connect_timeout=5,
        autocommit=True,
        row_factory=tuple_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SHOW server_version")
            version_row = cursor.fetchone()
            if version_row is None or len(version_row) != 1 or not isinstance(version_row[0], str):
                raise RuntimeError("PostgreSQL did not return its server version.")
            server_version = version_row[0]
        for trial_index in range(trials):
            order: tuple[VersionName, VersionName] = (
                ("uuid4", "uuid7") if trial_index % 2 == 0 else ("uuid7", "uuid4")
            )
            for version_name in order:
                results[version_name].append(
                    _run_trial(
                        connection,
                        generator=generators[version_name],
                        version_name=version_name,
                        trial_index=trial_index,
                        row_count=rows,
                    )
                )

    return {
        "benchmark": "uuid_generation_and_postgresql_insert",
        "status": "completed",
        "configuration": {
            "rows_per_trial": rows,
            "trials": trials,
            "payload_bytes": len(PAYLOAD.encode("utf-8")),
            "statement_timeout_ms": STATEMENT_TIMEOUT_MS,
            "insert_method": "COPY",
            "order": "alternating",
        },
        "runtime": {
            "python_version": platform.python_version(),
            "postgresql_version": server_version,
        },
        "results": {
            "uuid4": _version_results(results["uuid4"]),
            "uuid7": _version_results(results["uuid7"]),
        },
        "limitations": [
            "This benchmark uses temporary tables and synthetic rows only.",
            "It measures UUID generation and COPY insertion in one test database.",
            "It does not model application traffic, concurrent writers, or production data.",
            "Index size is the combined size of the UUID primary key and cycle/sequence indexes.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        _validate_options(arguments.rows, arguments.trials)
        database_url = _validate_environment(os.environ)
        report = benchmark(database_url, rows=arguments.rows, trials=arguments.trials)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (RuntimeError, psycopg.Error, OSError):
        print("The UUID benchmark failed. Sensitive details were omitted.", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
