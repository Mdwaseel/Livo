"""Stamp existing leave with the workspace of whoever it belongs to.

Leave is a fact about a person, so it inherits their partition. Company-wide
holidays (no user) are left NULL, which `core.tenancy.scope` reads as the home
workspace — and `employees.leave.holiday_dates` deliberately treats a NULL
holiday as applying everywhere, because a public holiday in India is a public
holiday for a partner's staff too.
"""
from django.db import migrations


def stamp(apps, schema_editor):
    LeaveRecord = apps.get_model("resource_planner", "LeaveRecord")
    User = apps.get_model("accounts", "User")

    by_workspace = {}
    for user_id, workspace_id in User.objects.values_list("pk", "workspace_id"):
        by_workspace.setdefault(workspace_id, []).append(user_id)

    for workspace_id, user_ids in by_workspace.items():
        if workspace_id is None:
            continue
        LeaveRecord.objects.filter(
            user_id__in=user_ids, workspace__isnull=True
        ).update(workspace_id=workspace_id)


def unstamp(apps, schema_editor):
    LeaveRecord = apps.get_model("resource_planner", "LeaveRecord")
    LeaveRecord.objects.update(workspace=None)


class Migration(migrations.Migration):

    dependencies = [
        ("resource_planner", "0003_leaverecord_decided_at_leaverecord_decision_note_and_more"),
        ("core", "0005_seed_home_workspace"),
    ]

    operations = [
        migrations.RunPython(stamp, unstamp),
    ]
