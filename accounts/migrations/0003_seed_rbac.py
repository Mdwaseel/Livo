"""Seed the RBAC catalog (modules, default roles + matrices) and backfill
users' primary_role from the deprecated role CharField. Idempotent — the live
`seed_rbac` management command shares the same logic for re-runs."""
from django.db import migrations


def seed(apps, schema_editor):
    from accounts.rbac_seed import seed_all
    seed_all(
        apps.get_model("accounts", "Module"),
        apps.get_model("accounts", "Role"),
        apps.get_model("accounts", "RolePermission"),
        apps.get_model("accounts", "User"),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_module_role_user_employee_code_user_reports_to_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
