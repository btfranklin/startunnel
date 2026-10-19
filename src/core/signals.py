"""Install database query metrics on each connection."""

from typing import Any

from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.signals import connection_created
from django.dispatch import receiver

from .metrics import measure_database_query


@receiver(connection_created, dispatch_uid="startunnel_database_metrics")
def database_connection_created(
    sender: object, connection: BaseDatabaseWrapper, **kwargs: Any
) -> None:
    """Install one query wrapper, including when this connection reconnects."""

    del sender, kwargs
    if measure_database_query not in connection.execute_wrappers:
        connection.execute_wrappers.append(measure_database_query)
