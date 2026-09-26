from django.contrib import admin

from .models import (
    ActivityCheckpoint,
    AuditEvent,
    Cycle,
    IdempotencyRecord,
    Message,
    MessageMention,
    Tunnel,
    TunnelAddress,
    TunnelEvent,
)


@admin.register(Tunnel)
class TunnelAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("id", "creator_id", "state", "created_at")


@admin.register(TunnelAddress)
class TunnelAddressAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("id", "tunnel_id", "state", "issued_at")
    exclude = ("address_digest",)


@admin.register(Cycle)
class CycleAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("id", "tunnel_id", "number", "state", "created_at", "closed_at")
    exclude = ("manifest_digest",)


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("id", "cycle_id", "sequence", "sender_id", "depth", "created_at")
    exclude = ("text_payload", "json_payload", "content_digest")


@admin.register(TunnelEvent)
class TunnelEventAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("id", "tunnel_id", "position", "event_type", "occurred_at")
    exclude = ("metadata",)


admin.site.register(IdempotencyRecord)
admin.site.register(AuditEvent)
admin.site.register(MessageMention)
admin.site.register(ActivityCheckpoint)
