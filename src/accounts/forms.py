"""Forms for local human account administration."""

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.exceptions import ValidationError

from .models import User


class ThrottledAuthenticationForm(AuthenticationForm):
    def clean(self) -> dict[str, str]:
        from api.rate_limits import consume_login_attempt
        from tunnels.errors import RateLimited

        username = str(self.data.get("username", ""))
        source_ip = str(
            self.request.META.get("REMOTE_ADDR", "unknown") if self.request else "unknown"
        )
        try:
            consume_login_attempt(source_ip=source_ip, username=username)
        except RateLimited as error:
            raise ValidationError(
                self.error_messages["invalid_login"],
                code="invalid_login",
                params={"username": self.username_field.verbose_name},
            ) from error
        return super().clean()


class InstanceUserCreationForm(UserCreationForm[User]):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username",)


class UserStateForm(forms.Form):
    user_id = forms.UUIDField(widget=forms.HiddenInput())
    active = forms.BooleanField(required=False, widget=forms.HiddenInput())
