"""Local account authentication views."""

from django.contrib.auth.views import LoginView
from django.http import HttpRequest
from django.http.response import HttpResponseBase

from .forms import ThrottledAuthenticationForm


def login(request: HttpRequest) -> HttpResponseBase:
    """Authenticate one local admin account without public registration."""

    return LoginView.as_view(
        template_name="account/login.html",
        authentication_form=ThrottledAuthenticationForm,
        redirect_authenticated_user=True,
    )(request)
