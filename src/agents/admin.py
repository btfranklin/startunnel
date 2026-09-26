from django.contrib import admin

from .models import AgentCredential


@admin.register(AgentCredential)
class AgentCredentialAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("name", "display_prefix", "created_by_id", "created_at", "revoked_at")
    exclude = ("key_digest",)
    search_fields = ("name", "display_prefix")
