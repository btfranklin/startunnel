"""Forms for local admin account administration."""

from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.password_validation import password_validators_help_text_html
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


class InstanceAdminCreationForm(UserCreationForm[User]):
    password1 = forms.CharField(
        label="Password",
        required=False,
        strip=False,
        widget=forms.PasswordInput(),
        help_text=password_validators_help_text_html(),
    )
    password2 = forms.CharField(
        label="Password confirmation", required=False, widget=forms.PasswordInput()
    )

    def clean(self) -> dict[str, str]:
        cleaned = super().clean() or {}
        if bool(cleaned.get("password1")) != bool(cleaned.get("password2")):
            self.add_error("password2", "Enter the same password in both fields.")
        return cleaned

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username",)


class AdminStateForm(forms.Form):
    admin_id = forms.UUIDField(widget=forms.HiddenInput())
    active = forms.BooleanField(required=False, widget=forms.HiddenInput())
    expected_active = forms.BooleanField(required=False, widget=forms.HiddenInput())
