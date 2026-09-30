"""Restrict who can ADD or DELETE the core records.

Create + delete on Clients, Projects, Employees and Documents become Manager /
Super Admin only; every other role keeps view/edit but loses add/remove. Bulk
employee import (also an "add") is locked the same way, and monthly-report
generation is limited to Super Admin / Manager / Project Manager.

Applied as a one-shot data migration so existing databases are tightened too —
seed_rbac never rewrites a role that already has permission rows, so the seed
change alone would only reach fresh installs. Runs after 0003 seeds the full
matrices; safe to run on an already-tightened database (it only clears flags).
"""
from django.db import migrations

CREATE_DELETE_LOCK = ("projects", "clients", "employees", "documents")
PROTECTED = ("Super Admin", "Manager")
REPORT_ROLES = ("Super Admin", "Manager", "Project Manager")


def tighten(apps, schema_editor):
    RolePermission = apps.get_model("accounts", "RolePermission")

    (RolePermission.objects
     .filter(module__key__in=CREATE_DELETE_LOCK)
     .exclude(role__name__in=PROTECTED)
     .update(can_create=False, can_delete=False))

    # Bulk-adding employees via import is still adding employees.
    (RolePermission.objects
     .filter(module__key="employees")
     .exclude(role__name__in=PROTECTED)
     .update(can_import=False))

    # Monthly-report generation (reports.create) stays with SA / Manager / PM.
    (RolePermission.objects
     .filter(module__key="reports")
     .exclude(role__name__in=REPORT_ROLES)
     .update(can_create=False))


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_seed_rbac")]
    operations = [migrations.RunPython(tighten, migrations.RunPython.noop)]
