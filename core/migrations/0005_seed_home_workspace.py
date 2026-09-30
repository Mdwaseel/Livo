"""Create the home workspace and stamp everything that predates partitioning.

`core.tenancy.scope` reads NULL as "home", so this backfill is not strictly
required for correctness — but leaving hundreds of rows relying on that fallback
would mean the fallback is the norm rather than the safety net, and the first
query someone writes by hand without it would quietly lose our own data.

Runs as a data migration rather than a management command so a fresh database —
including every test database — comes up with a home workspace already present.
Idempotent: re-running finds the workspace and stamps nothing.
"""
from django.db import migrations


def seed(apps, schema_editor):
    Workspace = apps.get_model("core", "Workspace")
    home = Workspace.objects.filter(is_home=True).order_by("pk").first()
    if home is None:
        AgencySettings = apps.get_model("core", "AgencySettings")
        name = (AgencySettings.objects.values_list("agency_name", flat=True).first()
                or "Livo Digital")
        home = Workspace.objects.create(
            name=name, slug="home", is_home=True, is_active=True,
            color="#00795B",
            notes="The agency that owns this install. Every record written "
                  "before partner workspaces existed belongs here.")

    # (app_label, model) for everything that carries a workspace column.
    for app_label, model_name in (
            ("core", "ActivityLog"), ("accounts", "User"),
            ("clients", "Client"), ("projects", "Project"),
            ("calendar_hub", "CalendarEvent")):
        model = apps.get_model(app_label, model_name)
        model.objects.filter(workspace__isnull=True).update(workspace=home)


def unseed(apps, schema_editor):
    """Reverse leaves the rows pointing at the workspace and only drops the
    marker, because clearing the columns would be indistinguishable from the
    forward migration never having run — and the FKs are dropped by the schema
    migration this one sits on top of anyway."""
    Workspace = apps.get_model("core", "Workspace")
    Workspace.objects.filter(slug="home", is_home=True).update(is_home=False)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_workspace_activitylog_workspace_and_more"),
        ("accounts", "0010_user_workspace"),
        ("clients", "0002_client_workspace_and_more"),
        ("projects", "0011_project_workspace_and_more"),
        ("calendar_hub", "0002_calendarevent_workspace_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
