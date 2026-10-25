"""Validate shared PostgreSQL connection settings for process listeners."""

from __future__ import annotations

from typing import TypedDict

from django.conf import settings


class PostgresConnectionOptions(TypedDict):
    dbname: str
    user: str | None
    password: str | None
    host: str | None
    port: str | int | None
    connect_timeout: int


def _optional_parameter(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("The listener database parameter must be text.")
    return value


def postgres_connection_options() -> PostgresConnectionOptions:
    database = settings.DATABASES["default"]
    name: object = database["NAME"]
    port: object = database.get("PORT") or None
    if not isinstance(name, str):
        raise ValueError("The listener database name must be text.")
    if port is not None and (isinstance(port, bool) or not isinstance(port, (str, int))):
        raise ValueError("The listener database port must be text or an integer.")
    return {
        "dbname": name,
        "user": _optional_parameter(database.get("USER")),
        "password": _optional_parameter(database.get("PASSWORD")),
        "host": _optional_parameter(database.get("HOST")),
        "port": port,
        "connect_timeout": 5,
    }
