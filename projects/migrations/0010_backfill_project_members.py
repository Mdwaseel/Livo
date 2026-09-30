"""Seed project membership from the work people have already done.

Membership starts empty, and an empty list means invisible to everyone without
`projects.view_all`. Shipping that as-is would blank the Projects page for every
Developer and Designer on the morning of the deploy.

So the first membership list is derived from the evidence already in the
database — the same definition resource_planner/selectors.py already uses to
answer "who is on this project", plus the people who raised the work:

    * anyone assigned a task on the project
    * anyone reviewing a task on the project
    * anyone who created a task on it
    * anyone who logged hours against it
    * whoever created the project

Runs once. New projects get their members from the UI and from being assigned
work (projects.access.ensure_member).
"""
from django.db import migrations


def backfill(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    Task = apps.get_model("projects", "Task")
    WorkLogEntry = apps.get_model("projects", "WorkLogEntry")

    for project in Project.objects.all().iterator():
        user_ids = set()

        for field in ("assignee_id", "reviewer_id", "created_by_id"):
            user_ids.update(
                Task.objects.filter(project=project)
                .exclude(**{field: None})
                .values_list(field, flat=True)
            )

        user_ids.update(
            WorkLogEntry.objects.filter(project=project)
            .exclude(logged_by=None)
            .values_list("logged_by_id", flat=True)
        )

        if project.created_by_id:
            user_ids.add(project.created_by_id)

        if user_ids:
            project.members.add(*user_ids)


def clear(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    for project in Project.objects.all().iterator():
        project.members.clear()


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0009_project_members"),
    ]

    operations = [
        migrations.RunPython(backfill, clear),
    ]
