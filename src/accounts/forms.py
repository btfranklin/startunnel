"""Forms for local human account administration."""

from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.exceptions import ValidationError

from core.security import client_ip

from .models import User


def _limit_login(form: AuthenticationForm) -> None:
    from api.rate_limits import consume_login_attempt
    from tunnels.errors import RateLimited

    username = str(form.data.get("username", ""))
    source = client_ip(form.request) if form.request else "unknown"
    try:
        consume_login_attempt(source_ip=source, username=username)
    except RateLimited as error:
        raise ValidationError(
            form.error_messages["invalid_login"],
            code="invalid_login",
            params={"username": form.username_field.verbose_name},
        ) from error


class ThrottledAuthenticationForm(AuthenticationForm):
    def clean(self) -> dict[str, str]:
        _limit_login(self)
        return super().clean()


class ThrottledAdminAuthenticationForm(AdminAuthenticationForm):
    def clean(self) -> dict[str, str]:
        _limit_login(self)
        return super().clean()


class InstanceUserCreationForm(UserCreationForm[User]):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username",)


class UserStateForm(forms.Form):
    user_id = forms.UUIDField(widget=forms.HiddenInput())
    active = forms.BooleanField(required=False, widget=forms.HiddenInput())
