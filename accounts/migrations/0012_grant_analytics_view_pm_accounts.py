"""Hand Analytics *viewing* back to Project Manager and Accounts.

Migration 0009 closed the reporting modules to everyone below Manager, and
`rbac_seed.py` has excluded them from both matrices ever since. That was a
deliberate call — analytics reports ACROSS people, which is the thing
per-assignee task visibility exists to limit — and it is still the right
default for Developer, Designer, Sales, HR and Client, who stay closed here.

What it also did, though, was leave a Project Manager unable to see the
delivery picture for projects they run, and Accounts unable to see the revenue
trend behind payments they record. Both already hold `view` on the underlying
modules, so the numbers are ones they can reach today by clicking through
project pages one at a time; analytics only saves them the clicking.

Scope is deliberately narrow:

* `can_view` only. `can_export` stays off — a dashboard is a glance, an export
  is a file of salary-adjacent productivity data that leaves the building.
* Analytics only. `resource_planning` stays Manager-and-above, because the
  planner board lists per-assignee tasks directly rather than aggregating them.
* Money is untouched. Revenue and outstanding are gated a second time by
  `finance.view`, so a PM gets delivery analytics with the money columns simply
  absent — the same split the project page already makes.

Idempotent, like every RBAC migration here: it sets one flag and leaves any
hand-edit in /settings/roles/ to the flags it does not name.
"""
from django.db import migrations

MODULE = "analytics"
GRANT_TO = ["Project Manager", "Accounts"]


def apply(apps, schema_editor):
    RolePermission = apps.get_model("accounts", "RolePermission")
    Module = apps.get_model("accounts", "Module")
    Role = apps.get_model("accounts", "Role")

    module = Module.objects.filter(key=MODULE).first()
    if module is None:
        # A database seeded before the module existed: seed_rbac creates it and
        # this migration is a no-op rather than a crash.
        return

    for role in Role.objects.filter(name__in=GRANT_TO):
        RolePermission.objects.update_or_create(
            role=role, module=module, defaults={"can_view": True},
        )


def unapply(apps, schema_editor):
    """Back to Manager-and-above, which is what 0009 left behind."""
    RolePermission = apps.get_model("accounts", "RolePermission")
    (RolePermission.objects
     .filter(module__key=MODULE, role__name__in=GRANT_TO)
     .update(can_view=False))


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0011_business_module"),
    ]

    operations = [
        migrations.RunPython(apply, unapply),
    ]
