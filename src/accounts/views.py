"""Local account authentication views."""

from uuid import uuid4

from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from tunnels.errors import TunnelDomainError

from .admin_services import execute_admin_operation
from .forms import ThrottledAuthenticationForm
from .models import User


def login(request: HttpRequest) -> HttpResponseBase:
    """Authenticate one local admin account without public registration."""

    return LoginView.as_view(
        template_name="account/login.html",
        authentication_form=ThrottledAuthenticationForm,
        redirect_authenticated_user=True,
    )(request)


@login_required
@require_http_methods(["GET", "POST"])
def password_change(request: HttpRequest) -> HttpResponse:
    """Change a browser password through the shared admin operation service."""
    user = request.user
    if not isinstance(user, User):
        raise PermissionDenied
    form = PasswordChangeForm(user, request.POST if request.method == "POST" else None)
    key = request.POST.get("idempotency_key", "") if request.method == "POST" else str(uuid4())
    if request.method == "POST" and form.is_valid():
        try:
            result = execute_admin_operation(
                actor=user,
                operation="accounts.password",
                key=key,
                data={"admin_id": str(user.id), "password": form.cleaned_data["new_password1"]},
            )
        except TunnelDomainError as error:
            form.add_error(None, error.safe_message)
        else:
            user.refresh_from_db()
            update_session_auth_hash(request, user)
            response = redirect("site:account")
            response["X-Startunnel-Operation-Id"] = result["operation"]["id"]
            response["X-Startunnel-Audit-Event-Id"] = result["operation"]["audit_event_id"]
            return response
    return render(
        request,
        "account/password_change.html",
        {"form": form, "idempotency_key": key or str(uuid4())},
        status=400 if request.method == "POST" else 200,
    )
