"""Public and browser-session routes."""

from django.urls import path

from . import admin_views, views
from .docs import DOCUMENTATION_PAGES

app_name = "site"

urlpatterns = [
    path("", views.home, name="home"),
    *[
        path(
            "docs/" if page.slug == "index" else f"docs/{page.slug}/",
            views.documentation,
            {"slug": page.slug},
            name="docs" if page.slug == "index" else f"docs-{page.slug}",
        )
        for page in DOCUMENTATION_PAGES
    ],
    path(
        "downloads/star_tunnel.py",
        views.download_agent_client,
        name="download-agent-client",
    ),
    path("app/", views.dashboard, name="dashboard"),
    path("app/admins/", views.admins, name="admins"),
    path("app/admins/create/", views.create_admin_account, name="create-admin"),
    path("app/admins/state/", views.set_admin_state, name="set-admin-state"),
    path("app/admins/access/", admin_views.admin_access, name="admin-access"),
    path("app/audit/", admin_views.audit, name="audit"),
    path("app/learn/", views.learn, name="learn"),
    path("app/agents/", views.agents, name="agents"),
    path("app/agents/create/", views.create_agent, name="create-agent"),
    path("app/agents/revoke/", views.revoke_agent, name="revoke-agent"),
    path("app/tunnels/", views.tunnels, name="tunnels"),
    path("app/account/", views.account, name="account"),
]
