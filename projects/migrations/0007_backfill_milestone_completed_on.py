"""Seed completed_on for milestones that were already DONE.

Monthly reports now read completed_on instead of updated_at. Existing done
milestones have no value there, so without this they'd vanish from reports
entirely. updated_at is the best evidence we have of when they landed — it's
exactly what the old query used — so it becomes the starting value.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    Milestone = apps.get_model("projects", "Milestone")
    rows = list(Milestone.objects.filter(status="DONE", completed_on__isnull=True))
    for m in rows:
        m.completed_on = m.updated_at.date()
    if rows:
        Milestone.objects.bulk_update(rows, ["completed_on"])


def unset(apps, schema_editor):
    Milestone = apps.get_model("projects", "Milestone")
    Milestone.objects.update(completed_on=None)


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0006_milestone_completed_on_recurringtask_anchor_day"),
    ]

    operations = [
        migrations.RunPython(backfill, unset),
    ]
