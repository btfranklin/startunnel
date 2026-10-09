"""Public and browser-session routes."""

from django.urls import path

from . import views

app_name = "site"

urlpatterns = [
    path("", views.home, name="home"),
    path("docs/", views.documentation, {"slug": "index"}, name="docs"),
    path(
        "docs/agent-quickstart/",
        views.documentation,
        {"slug": "agent-quickstart"},
        name="docs-agent-quickstart",
    ),
    path("docs/tutorial/", views.documentation, {"slug": "tutorial"}, name="docs-tutorial"),
    path("docs/concepts/", views.documentation, {"slug": "concepts"}, name="docs-concepts"),
    path(
        "docs/authentication/",
        views.documentation,
        {"slug": "authentication"},
        name="docs-authentication",
    ),
    path(
        "docs/instance-tunnels/",
        views.documentation,
        {"slug": "instance-tunnels"},
        name="docs-instance-tunnels",
    ),
    path(
        "docs/rotation-and-history/",
        views.documentation,
        {"slug": "rotation-and-history"},
        name="docs-rotation-and-history",
    ),
    path("docs/delivery/", views.documentation, {"slug": "delivery"}, name="docs-delivery"),
    path("docs/errors/", views.documentation, {"slug": "errors"}, name="docs-errors"),
    path("docs/limits/", views.documentation, {"slug": "limits"}, name="docs-limits"),
    path("docs/security/", views.documentation, {"slug": "security"}, name="docs-security"),
    path("docs/examples/", views.documentation, {"slug": "examples"}, name="docs-examples"),
    path(
        "docs/local-development/",
        views.documentation,
        {"slug": "local-development"},
        name="docs-local-development",
    ),
    path(
        "downloads/star_tunnel.py",
        views.download_agent_client,
        name="download-agent-client",
    ),
    path("app/", views.dashboard, name="dashboard"),
    path("app/admins/", views.admins, name="admins"),
    path("app/admins/create/", views.create_admin_account, name="create-admin"),
    path("app/admins/state/", views.set_admin_state, name="set-admin-state"),
    path("app/learn/", views.learn, name="learn"),
    path("app/agents/", views.agents, name="agents"),
    path("app/agents/create/", views.create_agent, name="create-agent"),
    path("app/agents/revoke/", views.revoke_agent, name="revoke-agent"),
    path("app/tunnels/", views.tunnels, name="tunnels"),
    path("app/account/", views.account, name="account"),
]
