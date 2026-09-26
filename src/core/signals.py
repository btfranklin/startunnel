"""Wake activity readers after committed tunnel events."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from tunnels.models import TunnelEvent

from .notifications import ACTIVITY_CHANNEL, notify


@receiver(
    post_save,
    sender=TunnelEvent,
    dispatch_uid="startunnel_activity_notification",
)
def tunnel_event_saved(sender: object, instance: TunnelEvent, **kwargs: object) -> None:
    del sender, kwargs
    notify(ACTIVITY_CHANNEL, str(instance.tunnel_id))
