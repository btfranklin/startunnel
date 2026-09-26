"""Local account behavior for one operator-managed instance."""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.test import Client

from accounts.forms import ThrottledAuthenticationForm
from accounts.models import User
from accounts.services import AccountError, create_human, require_active_human, set_human_active
from tunnels.models import AuditEvent

pytestmark = pytest.mark.django_db


def test_user_manager_normalizes_local_users_and_superusers() -> None:
    user = User.objects.create_user(username="  local-user  ", password="correct horse battery")
    assert user.username == "local-user"
    assert user.check_password("correct horse battery")
    assert str(user) == "local-user"
    admin = User.objects.create_superuser(username="root", password="correct horse battery")
    assert admin.is_staff and admin.is_superuser


def test_user_manager_rejects_invalid_creation() -> None:
    with pytest.raises(ValueError, match="username"):
        User.objects.create_user(username="   ")
    with pytest.raises(ValueError, match="staff"):
        User.objects.create_superuser(username="root", is_staff=False)
    with pytest.raises(ValueError, match="superuser"):
        User.objects.create_superuser(username="root", is_superuser=False)


def test_humans_can_create_and_manage_other_humans(user_factory: Any) -> None:
    actor = user_factory(username="operator")
    created = create_human(actor=actor, username="second", password="long-safe-password-42")
    assert created.check_password("long-safe-password-42")
    assert AuditEvent.objects.filter(action="human.created", target_id=created.id).exists()

    assert set_human_active(actor=actor, user_id=created.id, active=False).is_active is False
    assert AuditEvent.objects.filter(action="human.deactivated", target_id=created.id).exists()
    assert set_human_active(actor=actor, user_id=created.id, active=True).is_active is True
    assert AuditEvent.objects.filter(action="human.reactivated", target_id=created.id).exists()


def test_account_services_reject_inactive_or_missing_accounts(user_factory: Any) -> None:
    actor = user_factory()
    actor.is_active = False
    actor.save(update_fields=["is_active"])
    with pytest.raises(AccountError, match="inactive"):
        require_active_human(actor)
    with pytest.raises(AccountError, match="inactive"):
        create_human(actor=actor, username="new", password="long-safe-password-42")
    with pytest.raises(AccountError, match="not available"):
        set_human_active(actor=actor, user_id=actor.id, active=True)


def test_last_active_human_cannot_be_deactivated(user_factory: Any) -> None:
    actor = user_factory()
    with pytest.raises(AccountError, match="last active"):
        set_human_active(actor=actor, user_id=actor.id, active=False)
    assert set_human_active(actor=actor, user_id=actor.id, active=True) == actor


def test_local_login_logout_and_safe_redirects(client: Client, user_factory: Any) -> None:
    user_factory(username="operator", password="test-password-42")
    response = client.post(
        "/accounts/login/?next=https://attacker.invalid/",
        {"username": "operator", "password": "test-password-42"},
    )
    assert response.status_code == 302
    assert response["Location"] == "/app/"
    assert client.get("/app/").status_code == 200
    assert client.get("/accounts/logout/").status_code == 405
    assert client.post("/accounts/logout/").status_code == 302
    assert client.get("/app/").status_code == 302


def test_login_form_maps_rate_limit_to_generic_authentication_error(
    rf: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tunnels.errors import RateLimited

    def limited(**kwargs: Any) -> None:
        assert kwargs == {"source_ip": "192.0.2.4", "username": "Someone"}
        raise RateLimited()

    monkeypatch.setattr("api.rate_limits.consume_login_attempt", limited)
    request = rf.post("/accounts/login/", REMOTE_ADDR="192.0.2.4")
    form = ThrottledAuthenticationForm(
        request=request,
        data={"username": "Someone", "password": "not-the-password"},
    )
    assert not form.is_valid()
    assert "correct username and password" in form.errors["__all__"][0]


def test_instance_admin_command_creates_plain_product_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["strong local password 42", "strong local password 42"])
    monkeypatch.setattr(
        "accounts.management.commands.create_instance_admin.getpass", lambda _: next(answers)
    )
    output = StringIO()
    call_command("create_instance_admin", "operator", stdout=output)
    user = User.objects.get(username="operator")
    assert user.is_active and not user.is_staff and not user.is_superuser
    assert user.check_password("strong local password 42")
    assert "Created local administrator" in output.getvalue()


@pytest.mark.parametrize(
    ("username", "answers", "message"),
    [
        ("   ", [], "username"),
        ("operator", ["first password 42", "different password 42"], "do not match"),
        ("operator", ["short", "short"], "too short"),
    ],
)
def test_instance_admin_command_rejects_bad_input(
    username: str,
    answers: list[str],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = iter(answers)
    monkeypatch.setattr(
        "accounts.management.commands.create_instance_admin.getpass", lambda _: next(values)
    )
    with pytest.raises(CommandError, match=message):
        call_command("create_instance_admin", username)


def test_instance_admin_command_rejects_duplicate(
    user_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_factory(username="operator")
    monkeypatch.setattr(
        "accounts.management.commands.create_instance_admin.getpass", lambda _: "unused"
    )
    with pytest.raises(CommandError, match="already exists"):
        call_command("create_instance_admin", "OPERATOR")


def test_user_pages_create_and_change_accounts(client: Client, user_factory: Any) -> None:
    actor = user_factory(username="operator")
    client.force_login(actor)
    assert client.get("/app/users/").status_code == 200
    created = client.post(
        "/app/users/create/",
        {
            "username": "colleague",
            "password1": "strong local password 42",
            "password2": "strong local password 42",
        },
    )
    assert created.status_code == 302
    colleague = User.objects.get(username="colleague")
    assert (
        client.post("/app/users/state/", {"user_id": colleague.id, "active": ""}).status_code == 302
    )
    colleague.refresh_from_db()
    assert colleague.is_active is False
    assert client.post("/app/users/state/", {"user_id": "bad"}).status_code == 404


def test_user_create_page_returns_form_errors(client: Client, user_factory: Any) -> None:
    client.force_login(user_factory())
    response = client.post(
        "/app/users/create/",
        {"username": "new", "password1": "different", "password2": "values"},
    )
    assert response.status_code == 400
    assert not User.objects.filter(username="new").exists()
