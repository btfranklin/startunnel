"""Keep Django model administration available for inspection only."""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.db.models import Model
from django.http import HttpRequest

from .models import AgentReceipt, AuditEvent

if TYPE_CHECKING:
    _InspectionBase = admin.ModelAdmin[Model]
else:
    _InspectionBase = admin.ModelAdmin


class InspectionAdmin(_InspectionBase):
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False


admin.site.register(AgentReceipt, InspectionAdmin)
admin.site.register(AuditEvent, InspectionAdmin)
