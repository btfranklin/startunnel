"""Wake activity readers after committed tunnel events."""

from typing import Any

from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.signals import connection_created
from django.db.models.signals import post_save
from django.dispatch import receiver

from tunnels.models import TunnelEvent

from .metrics import measure_database_query
from .notifications import ACTIVITY_CHANNEL, notify


@receiver(
    post_save,
    sender=TunnelEvent,
    dispatch_uid="startunnel_activity_notification",
)
def tunnel_event_saved(sender: object, instance: TunnelEvent, **kwargs: object) -> None:
    del sender, kwargs
    notify(ACTIVITY_CHANNEL, str(instance.tunnel_id))


@receiver(connection_created, dispatch_uid="startunnel_database_metrics")
def database_connection_created(
    sender: object, connection: BaseDatabaseWrapper, **kwargs: Any
) -> None:
    """Install one query wrapper, including when this connection reconnects."""

    del sender, kwargs
    if measure_database_query not in connection.execute_wrappers:
        connection.execute_wrappers.append(measure_database_query)
