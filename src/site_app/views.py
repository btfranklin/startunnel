"""HTML-first public and browser-session views."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.forms import InstanceUserCreationForm, UserStateForm
from accounts.models import User
from accounts.services import AccountError, create_human, set_human_active
from agents.models import AgentCredential
from agents.services import (
    CredentialError,
    create_credential,
    revoke_credential,
)
from tunnels.errors import InvalidRequest, TunnelDomainError, TunnelUnavailable
from tunnels.models import Cycle, Tunnel, TunnelAddress
from tunnels.services import (
    close_cycle_as_operator,
    retire_tunnel_as_operator,
    retirement_confirmation,
    rollover_cycle_as_operator,
    rotate_address_as_operator,
    start_cycle_as_operator,
)

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
def users(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "app/users.html",
        {
            "create_form": InstanceUserCreationForm(),
            "users": User.objects.order_by("username", "id"),
        },
    )


@login_required
@require_POST
def create_user(request: HttpRequest) -> HttpResponse:
    actor = _authenticated_user(request)
    form = InstanceUserCreationForm(request.POST)
    if form.is_valid():
        try:
            create_human(
                actor=actor,
                username=form.cleaned_data["username"],
                password=form.cleaned_data["password1"],
            )
        except AccountError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, "The human account was created.")
            return redirect("site:users")
    return render(
        request,
        "app/users.html",
        {"create_form": form, "users": User.objects.order_by("username", "id")},
        status=400,
    )


@login_required
@require_POST
def set_user_state(request: HttpRequest) -> HttpResponse:
    actor = _authenticated_user(request)
    form = UserStateForm(request.POST)
    if not form.is_valid():
        raise Http404
    try:
        set_human_active(
            actor=actor,
            user_id=form.cleaned_data["user_id"],
            active=form.cleaned_data["active"],
        )
    except AccountError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "The human account state was updated.")
    return redirect("site:users")


def _one_time_key_response(
    request: HttpRequest,
    *,
    key: str,
    agent_name: str,
    subject_name: str,
    return_url: str,
) -> HttpResponse:
    response = render(
        request,
        "app/one_time_key.html",
        {
            "agent_key": key,
            "agent_name": agent_name,
            "subject_name": subject_name,
            "return_url": return_url,
        },
    )
    response.headers["Cache-Control"] = "no-store, private"
    return response


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
        {"form": AgentCredentialForm(), "credentials": _credentials()},
    )


@never_cache
@login_required
@require_POST
def create_agent(request: HttpRequest) -> HttpResponse:
    user = _authenticated_user(request)
    form = AgentCredentialForm(request.POST)
    if form.is_valid():
        try:
            issued = create_credential(
                actor=user,
                name=form.cleaned_data["name"],
            )
        except CredentialError as error:
            form.add_error(None, str(error))
        else:
            return _one_time_key_response(
                request,
                key=issued.key,
                agent_name=issued.credential.name,
                subject_name="Instance agent",
                return_url=reverse("site:agents"),
            )
    return render(
        request,
        "app/agents.html",
        {"form": form, "credentials": _credentials()},
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
        revoke_credential(actor=user, credential=credential)
    except CredentialError as error:
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
            if action == "close":
                close_cycle_as_operator(
                    actor=user,
                    tunnel_id=tunnel_id,
                    expected_cycle_id=UUID(request.POST.get("expected_cycle_id", "")),
                )
                messages.success(request, "Closed the current cycle. The tunnel is now dormant.")
            elif action in {"start", "rollover"}:
                form = TunnelCycleForm(request.POST)
                if not form.is_valid():
                    raise InvalidRequest("Enter a valid root message and cycle lifetime.")
                arguments = {
                    "actor": user,
                    "tunnel_id": tunnel_id,
                    "expected_address_generation": int(
                        request.POST.get("expected_address_generation", "")
                    ),
                    "root_content": {"type": "text", "text": form.cleaned_data["root_text"]},
                    "cycle_label": form.cleaned_data["cycle_label"],
                    "expires_in_seconds": form.cleaned_data["expires_in_seconds"],
                }
                if action == "start":
                    start_cycle_as_operator(**arguments)
                    messages.success(request, "Started a new cycle under the same address.")
                else:
                    rollover_cycle_as_operator(
                        **arguments,
                        expected_cycle_id=UUID(request.POST.get("expected_cycle_id", "")),
                    )
                    messages.success(request, "Closed the old cycle and started the next cycle.")
            elif action == "rotate":
                rotated = rotate_address_as_operator(
                    actor=user,
                    tunnel_id=tunnel_id,
                    expected_address_generation=int(
                        request.POST.get("expected_address_generation", "")
                    ),
                )
                return _one_time_address_response(
                    request,
                    address=rotated.address,
                    display_value=rotated.display_address,
                    generation=rotated.generation,
                    subject_name="Tunnel",
                    return_url=reverse("site:tunnels"),
                )
            elif action == "retire":
                retire_tunnel_as_operator(
                    actor=user,
                    tunnel_id=tunnel_id,
                    expected_address_generation=int(
                        request.POST.get("expected_address_generation", "")
                    ),
                    confirmation=request.POST.get("confirmation", ""),
                )
                messages.success(request, "Retired the tunnel and its current address.")
            else:
                raise InvalidRequest("The tunnel action is not valid.")
        except ValueError:
            messages.error(request, "That tunnel is not available.")
        except TunnelUnavailable:
            messages.error(request, "That tunnel is not available.")
        except TunnelDomainError as error:
            messages.error(request, error.safe_message)
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
    return render(request, "app/tunnels.html", {"tunnel_rows": tunnel_rows})


@login_required
def account(request: HttpRequest) -> HttpResponse:
    return render(request, "app/account.html")
