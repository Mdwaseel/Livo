"""Add the `resource_planning` module to the RBAC catalog and grant it.

`accounts/rbac_seed.py` writes a role's matrix only when that role has zero
permission rows, so changing the seed alone reaches fresh installs and nothing
else. This migration is what makes the module real on databases that already
exist.

Grants match the brief — Super Admin, Manager and Project Manager plan
resources — plus HR read-only, because HR records the leave the boards read and
needs to see the effect of their own entries. Everyone else gets nothing.

Idempotent: re-running only sets flags that are already correct.
"""
from django.db import migrations

MODULE_KEY = "resource_planning"
MODULE_LABEL = "Resource Planning"

ALL_ACTIONS = ("view", "create", "edit", "delete", "approve",
               "export", "import", "assign", "archive", "restore")

# role name -> actions granted
GRANTS = {
    "Super Admin": ALL_ACTIONS,
    "Manager": ALL_ACTIONS,
    "Project Manager": ("view", "create", "edit", "export"),
    "HR": ("view",),
}


def add_module(apps, schema_editor):
    Module = apps.get_model("accounts", "Module")
    Role = apps.get_model("accounts", "Role")
    RolePermission = apps.get_model("accounts", "RolePermission")

    # Sits next to Analytics in the matrix UI, which orders by `order` then
    # label; Analytics is seeded at 21 in the catalog list.
    module, _ = Module.objects.get_or_create(
        key=MODULE_KEY, defaults={"label": MODULE_LABEL, "order": 21})

    for role_name, actions in GRANTS.items():
        role = Role.objects.filter(name=role_name).first()
        if role is None:
            continue  # a renamed or deleted role is not this migration's problem
        permission, _ = RolePermission.objects.get_or_create(
            role=role, module=module)
        for action in actions:
            setattr(permission, f"can_{action}", True)
        permission.save()


def remove_module(apps, schema_editor):
    Module = apps.get_model("accounts", "Module")
    # Cascades to the RolePermission rows created above.
    Module.objects.filter(key=MODULE_KEY).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("resource_planner", "0001_initial"),
        ("accounts", "0003_seed_rbac"),
    ]
    operations = [migrations.RunPython(add_module, remove_module)]
