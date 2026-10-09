"""Project URL routes."""

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from accounts import views as account_views
from api.router import api
from api.views import api_docs
from core import views as core_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", account_views.login, name="account_login"),
    path(
        "accounts/logout/",
        auth_views.LogoutView.as_view(template_name="account/logout.html"),
        name="account_logout",
    ),
    path(
        "accounts/password/change/",
        account_views.password_change,
        name="password_change",
    ),
    path("api/docs/", api_docs, name="api-docs"),
    path("api/", api.urls),
    path("health/live", core_views.health_live, name="health-live"),
    path("health/ready", core_views.health_ready, name="health-ready"),
    path("metrics", core_views.metrics, name="metrics"),
    path("", include("site_app.urls")),
]
