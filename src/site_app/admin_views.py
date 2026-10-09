"""Server-rendered administrator key, password, and audit controls."""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST, require_safe

from accounts.admin_services import execute_admin_operation, list_admin_resources
from accounts.models import User
from accounts.services import AccountError
from tunnels.errors import TunnelDomainError


def private_download(
    secret: str, filename: str, operation: dict[str, str] | None = None
) -> HttpResponse:
    response = HttpResponse(secret + "\n", content_type="application/octet-stream")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "no-store, private"
    response["X-Content-Type-Options"] = "nosniff"
    if operation:
        response["X-Startunnel-Operation-Id"] = operation["id"]
        response["X-Startunnel-Audit-Event-Id"] = operation["audit_event_id"]
    return response


def browser_operation(request: HttpRequest, operation: str, data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request.user, User):
        raise Http404
    return execute_admin_operation(
        actor=request.user,
        operation=operation,
        key=request.POST.get("idempotency_key", ""),
        data=data,
    )


@login_required
@require_POST
def admin_access(request: HttpRequest) -> HttpResponse:
    action = request.POST.get("action")
    admin_id = request.POST.get("admin_id", str(request.user.pk))
    try:
        if action == "create-key":
            result = browser_operation(
                request,
                "keys.create",
                {
                    "admin_id": admin_id,
                    "name": request.POST.get("name", ""),
                    "expires_at": request.POST.get("expires_at") or None,
                },
            )
            return private_download(
                result["secret"], "startunnel-admin-key.txt", result["operation"]
            )
        if action == "revoke-key":
            browser_operation(request, "keys.revoke", {"key_id": request.POST.get("key_id", "")})
            messages.success(request, "The admin key was revoked.")
        elif action in {"set-password", "remove-password"}:
            password = request.POST.get("password", "") if action == "set-password" else None
            browser_operation(
                request, "accounts.password", {"admin_id": admin_id, "password": password}
            )
            messages.success(request, "Browser access was updated. Sign in again if required.")
        else:
            raise Http404
    except (AccountError, TunnelDomainError, ValueError) as error:
        messages.error(request, str(error))
    return redirect("site:admins")


@login_required
@require_safe
def audit(request: HttpRequest) -> HttpResponse:
    actor = request.user
    if not isinstance(actor, User):
        raise Http404
    try:
        events = list_admin_resources(
            "audit",
            actor=actor,
            limit=50,
            cursor=request.GET.get("cursor"),
            action=request.GET.get("action") or None,
        )
    except (AccountError, TunnelDomainError, ValueError) as error:
        messages.error(request, str(error))
        events = {"items": [], "next_cursor": None}
    return render(
        request,
        "app/audit.html",
        {"events": events, "action_filter": request.GET.get("action", "")},
    )
