from django.urls import path

from . import views, views_business, views_executive, views_workspaces

app_name = "core"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("activity/", views.activity, name="activity"),
    path("notifications/", views.notifications, name="notifications"),
    path("settings/", views.agency_settings, name="settings"),

    # --- the confidential money view (business.view only) ---
    path("business/", views_business.overview, name="business"),

    # --- the executive rollup ---
    # It renders at "/" now (see core.views.dashboard), so this is only here to
    # forward links and bookmarks from the one release where it had its own URL.
    path("executive/", views_executive.legacy_redirect, name="executive"),
    # Fetched by the page after it renders, never during — see views_executive.
    path("executive/brief.json", views_executive.brief_json,
         name="executive_brief"),

    # --- workspaces (partner access) ---
    path("settings/workspaces/", views_workspaces.workspace_list,
         name="workspace_list"),
    path("settings/workspaces/new/", views_workspaces.workspace_form,
         name="workspace_create"),
    path("settings/workspaces/<int:pk>/", views_workspaces.workspace_form,
         name="workspace_edit"),
    path("settings/workspaces/assign/", views_workspaces.workspace_assign,
         name="workspace_assign"),
    path("workspace/switch/", views_workspaces.workspace_switch,
         name="workspace_switch"),
]
