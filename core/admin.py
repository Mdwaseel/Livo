from django.contrib import admin
from .models import ActivityLog, AgencySettings, Workspace


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    """Django admin is a back door for the workspace screens at
    /settings/workspaces/, not a replacement — it has no notion of who is a
    home admin. `is_home` is read-only here because flipping it moves every
    unstamped row in the database to a different partition."""
    list_display = ("name", "slug", "is_home", "is_active", "created_at")
    list_filter = ("is_home", "is_active")
    search_fields = ("name", "slug")
    readonly_fields = ("is_home", "created_at")


@admin.register(AgencySettings)
class AgencySettingsAdmin(admin.ModelAdmin):
    list_display = ("agency_name", "email", "phone", "gstin")


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "workspace", "verb", "target")
    list_filter = ("verb", "workspace")
    search_fields = ("target", "description")
