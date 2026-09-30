"""Add the `business` module and grant it to Super Admin only.

The Business overview is the confidential money screen — revenue, outstanding,
collection rate, the figures that used to sit on everybody's dashboard. It gets
its own module key rather than reusing `finance.view` so that moving it off the
dashboard actually narrows who can see it: plenty of roles hold `finance.view`
because they need to read an invoice, and none of them should inherit the
agency's cash position along with it.

Granted to Super Admin and nobody else. Handing it to a named person is a tick
in /settings/roles/ — either on their role, or on a role created for the
purpose and attached as an extra role.

Idempotent: re-running finds the module and changes nothing.
"""
from django.db import migrations

MODULE_KEY = "business"
MODULE_LABEL = "Business Overview"
ACTIONS = ("view", "create", "edit", "delete", "approve", "export", "import",
           "assign", "archive", "restore", "view_all")


def add_module(apps, schema_editor):
    Module = apps.get_model("accounts", "Module")
    Role = apps.get_model("accounts", "Role")
    RolePermission = apps.get_model("accounts", "RolePermission")

    module, _ = Module.objects.get_or_create(
        key=MODULE_KEY,
        defaults={"label": MODULE_LABEL, "order": 26})

    available = {f.name for f in RolePermission._meta.get_fields()}
    for role in Role.objects.filter(name="Super Admin"):
        RolePermission.objects.get_or_create(
            role=role, module=module,
            defaults={f"can_{a}": True for a in ACTIONS
                      if f"can_{a}" in available})


def drop_module(apps, schema_editor):
    Module = apps.get_model("accounts", "Module")
    Module.objects.filter(key=MODULE_KEY).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0010_user_workspace"),
    ]

    operations = [
        migrations.RunPython(add_module, drop_module),
    ]
