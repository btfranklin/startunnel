"""HTML-first public and browser-session views."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.admin_services import admin_status, execute_admin_operation, list_admin_resources
from accounts.forms import AdminStateForm, InstanceAdminCreationForm
from accounts.models import User
from accounts.services import AccountError
from agents.models import AgentCredential
from agents.services import CredentialError
from tunnels.codec import display_address
from tunnels.errors import InvalidRequest, TunnelDomainError, TunnelUnavailable
from tunnels.models import Cycle, Tunnel, TunnelAddress
from tunnels.services import retirement_confirmation

from .admin_views import private_download
from .docs import DOCUMENTATION_PAGES, render_documentation
from .forms import (
    AgentCredentialForm,
    TunnelCycleForm,
)


def home(request: HttpRequest) -> HttpResponse:
    return redirect("site:dashboard")


def documentation(request: HttpRequest, slug: str) -> HttpResponse:
    try:
        page, content = render_documentation(slug)
    except KeyError as error:
        raise Http404 from error
    return render(
        request,
        "site/documentation.html",
        {"doc_page": page, "doc_pages": DOCUMENTATION_PAGES, "doc_content": content},
    )


@require_safe
def download_agent_client(request: HttpRequest) -> FileResponse:
    source_path = settings.PROJECT_ROOT / "cli" / "star_tunnel.py"
    if not source_path.is_file():
        raise Http404
    response = FileResponse(
        source_path.open("rb"),
        as_attachment=True,
        filename="star_tunnel.py",
        content_type="text/x-python",
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _authenticated_user(request: HttpRequest) -> User:
    if not isinstance(request.user, User):
        raise PermissionDenied
    return request.user


def _credentials() -> Any:
    return AgentCredential.objects.order_by("name", "id")


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    credentials = _credentials()
    active_credentials = credentials.available()
    return render(
        request,
        "app/dashboard.html",
        {
            "active_agent_count": active_credentials.count(),
            "active_tunnel_count": Tunnel.objects.exclude(state=Tunnel.State.RETIRED).count(),
            "admin_status": admin_status(),
            "recent_events": list_admin_resources(
                "audit", actor=_authenticated_user(request), limit=5
            )["items"],
            "next_agent_name": (
                "Tutorial sender" if not active_credentials.exists() else "Tutorial receiver"
            ),
        },
    )


@login_required
def learn(request: HttpRequest) -> HttpResponse:
    credentials = list(_credentials().available().values("name"))
    return render(request, "app/learn.html", {"credentials": credentials})


@login_required
@require_safe
def admins(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "app/admins.html",
        {
            "create_form": InstanceAdminCreationForm(),
            "admins": User.objects.prefetch_related("admin_credentials").order_by("username", "id"),
            "idempotency_key": str(uuid4()),
        },
    )


@login_required
@require_POST
def create_admin_account(request: HttpRequest) -> HttpResponse:
    actor = _authenticated_user(request)
    form = InstanceAdminCreationForm(request.POST)
    if form.is_valid():
        try:
            result = execute_admin_operation(
                actor=actor,
                operation="accounts.create",
                key=request.POST.get("idempotency_key", ""),
                data={
                    "username": form.cleaned_data["username"],
                    "password": form.cleaned_data["password1"] or None,
                },
            )
        except (AccountError, TunnelDomainError) as error:
            form.add_error(None, str(error))
        else:
            if result.get("secret"):
                return private_download(
                    result["secret"], "startunnel-admin-key.txt", result["operation"]
                )
            messages.success(request, "The admin account was created.")
            return redirect("site:admins")
    return render(
        request,
        "app/admins.html",
        {
            "create_form": form,
            "admins": User.objects.prefetch_related("admin_credentials").order_by("username", "id"),
            "idempotency_key": request.POST.get("idempotency_key") or str(uuid4()),
        },
        status=400,
    )


@login_required
@require_POST
def set_admin_state(request: HttpRequest) -> HttpResponse:
    actor = _authenticated_user(request)
    form = AdminStateForm(request.POST)
    if not form.is_valid():
        raise Http404
    try:
        if request.POST.get("expected_active") not in {"on", ""}:
            raise InvalidRequest("Reload the account page before changing its state.")
        execute_admin_operation(
            actor=actor,
            operation="accounts.state",
            key=request.POST.get("idempotency_key", ""),
            data={
                "admin_id": str(form.cleaned_data["admin_id"]),
                "active": form.cleaned_data["active"],
                "expected_active": request.POST.get("expected_active") == "on",
            },
        )
    except (AccountError, TunnelDomainError) as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "The admin account state was updated.")
    return redirect("site:admins")


def _one_time_address_response(
    request: HttpRequest,
    *,
    address: str,
    display_value: str,
    generation: int,
    subject_name: str,
    return_url: str,
) -> HttpResponse:
    response = render(
        request,
        "app/one_time_address.html",
        {
            "address": address,
            "display_address": display_value,
            "generation": generation,
            "subject_name": subject_name,
            "return_url": return_url,
        },
    )
    response.headers["Cache-Control"] = "no-store, private"
    return response


@login_required
@require_http_methods(["GET"])
def agents(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "app/agents.html",
        {
            "form": AgentCredentialForm(),
            "credentials": _credentials(),
            "idempotency_key": str(uuid4()),
        },
    )


@never_cache
@login_required
@require_POST
def create_agent(request: HttpRequest) -> HttpResponse:
    user = _authenticated_user(request)
    form = AgentCredentialForm(request.POST)
    if form.is_valid():
        try:
            result = execute_admin_operation(
                actor=user,
                operation="agents.create",
                key=request.POST.get("idempotency_key", ""),
                data={"name": form.cleaned_data["name"]},
            )
        except (AccountError, CredentialError, TunnelDomainError) as error:
            form.add_error(None, str(error))
        else:
            return private_download(
                result["secret"], "startunnel-agent-key.txt", result["operation"]
            )
    return render(
        request,
        "app/agents.html",
        {
            "form": form,
            "credentials": _credentials(),
            "idempotency_key": request.POST.get("idempotency_key") or str(uuid4()),
        },
        status=400,
    )


@login_required
@require_POST
def revoke_agent(request: HttpRequest) -> HttpResponseRedirect:
    user = _authenticated_user(request)
    credential = get_object_or_404(
        AgentCredential,
        pk=request.POST.get("credential_id"),
    )
    try:
        execute_admin_operation(
            actor=user,
            operation="agents.revoke",
            key=request.POST.get("idempotency_key", ""),
            data={"credential_id": str(credential.id)},
        )
    except (AccountError, CredentialError, TunnelDomainError) as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"Revoked {credential.name}.")
    return redirect("site:agents")


@login_required
@require_http_methods(["GET", "POST"])
def tunnels(request: HttpRequest) -> HttpResponse:
    user = _authenticated_user(request)
    if request.method == "POST":
        action = request.POST.get("action", "close")
        try:
            tunnel_id = UUID(request.POST.get("tunnel_id", ""))
            data: dict[str, Any] = {"tunnel_id": str(tunnel_id)}
            if action in {"close", "rollover"}:
                data["expected_cycle_id"] = str(UUID(request.POST.get("expected_cycle_id", "")))
            if action in {"start", "rollover", "rotate", "retire"}:
                data["expected_address_generation"] = int(
                    request.POST.get("expected_address_generation", "")
                )
            if action in {"start", "rollover"}:
                form = TunnelCycleForm(request.POST)
                if not form.is_valid():
                    raise InvalidRequest("Enter a valid root message and cycle lifetime.")
                data.update(
                    {
                        "root_content": {"type": "text", "text": form.cleaned_data["root_text"]},
                        "cycle_label": form.cleaned_data["cycle_label"],
                        "expires_in_seconds": form.cleaned_data["expires_in_seconds"],
                    }
                )
            if action == "retire":
                data["confirmation"] = request.POST.get("confirmation", "")
            if action in {"close", "start", "rollover", "rotate", "retire"}:
                result = execute_admin_operation(
                    actor=user,
                    operation=f"tunnels.{action}",
                    key=request.POST.get("idempotency_key", ""),
                    data=data,
                )
                if action == "rotate":
                    return _one_time_address_response(
                        request,
                        address=result["secret"],
                        display_value=display_address(result["secret"]),
                        generation=result["resource"]["address_generation"],
                        subject_name="Tunnel",
                        return_url=reverse("site:tunnels"),
                    )
                messages.success(request, "The tunnel operation is complete.")
            else:
                raise InvalidRequest("The tunnel action is not valid.")
        except ValueError:
            messages.error(request, "That tunnel is not available.")
        except TunnelUnavailable:
            messages.error(request, "That tunnel is not available.")
        except (AccountError, TunnelDomainError) as error:
            messages.error(request, str(error))
        return redirect("site:tunnels")

    tunnels = (
        Tunnel.objects.select_related("creator")
        .exclude(state=Tunnel.State.RETIRED)
        .order_by("-created_at", "id")[:200]
    )
    tunnel_rows = [
        {
            "tunnel": tunnel,
            "is_active": tunnel.state == Tunnel.State.ACTIVE,
            "cycle": tunnel.cycles.filter(state=Cycle.State.ACTIVE).first(),
            "cycles": tunnel.cycles.select_related("root_message").order_by("-number")[:10],
            "current_address": tunnel.addresses.filter(state=TunnelAddress.State.CURRENT).first(),
            "retirement_confirmation": retirement_confirmation(tunnel),
        }
        for tunnel in tunnels
    ]
    return render(
        request,
        "app/tunnels.html",
        {
            "tunnel_rows": tunnel_rows,
            "cycle_form": TunnelCycleForm(),
            "idempotency_key": str(uuid4()),
        },
    )


@login_required
def account(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "app/account.html",
        {
            "admin_keys": _authenticated_user(request).admin_credentials.all(),
            "idempotency_key": str(uuid4()),
        },
    )
