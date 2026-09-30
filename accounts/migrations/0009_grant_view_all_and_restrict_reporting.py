"""Turn on row-level visibility for existing installs.

The seed in rbac_seed.py only reaches a fresh database — it writes a role's
matrix once and then leaves it alone so UI edits survive a re-seed. Everything
here therefore has to be applied to already-populated installs by hand.

Three changes, all idempotent:

1. Grant `view_all` on projects and tasks to Super Admin and Manager, so the
   people who could see everything yesterday still can today.
2. Revoke `view_all` everywhere else, in case a matrix was hand-edited before
   this shipped.
3. Close Analytics and Resource Planning to everyone below Manager. Both report
   across people, so leaving them open would let a Developer read from the
   planner board exactly the task list the project page now hides.
"""
from django.db import migrations

BYPASS_ROLES = ["Super Admin", "Manager"]
SCOPED_MODULES = ["projects", "tasks"]
REPORTING_MODULES = ["analytics", "resource_planning"]
ACTION_FLAGS = [
    "can_view", "can_create", "can_edit", "can_delete", "can_approve",
    "can_export", "can_import", "can_assign", "can_archive", "can_restore",
    "can_view_all",
]


def apply(apps, schema_editor):
    RolePermission = apps.get_model("accounts", "RolePermission")
    Module = apps.get_model("accounts", "Module")
    Role = apps.get_model("accounts", "Role")

    # 1 + 2 — who bypasses row-level filtering.
    scoped = Module.objects.filter(key__in=SCOPED_MODULES)
    RolePermission.objects.filter(module__in=scoped).update(can_view_all=False)
    for role in Role.objects.filter(name__in=BYPASS_ROLES):
        for module in scoped:
            RolePermission.objects.update_or_create(
                role=role, module=module,
                defaults={"can_view": True, "can_view_all": True},
            )

    # 3 — reporting modules become Manager-and-above.
    reporting = Module.objects.filter(key__in=REPORTING_MODULES)
    (RolePermission.objects
     .filter(module__in=reporting)
     .exclude(role__name__in=BYPASS_ROLES)
     .update(**{flag: False for flag in ACTION_FLAGS}))
    for role in Role.objects.filter(name__in=BYPASS_ROLES):
        for module in reporting:
            RolePermission.objects.update_or_create(
                role=role, module=module,
                defaults={flag: True for flag in ACTION_FLAGS},
            )


def unapply(apps, schema_editor):
    """Back out to 'everyone sees everything'.

    Reversing cannot restore the exact matrix that existed before — nothing
    recorded it — so it does the safe, legible thing: drops every view_all flag.
    The old Analytics/Planning grants are not restored; re-run seed_rbac or set
    them in /settings/roles/ if you roll back.
    """
    RolePermission = apps.get_model("accounts", "RolePermission")
    RolePermission.objects.filter(
        module__key__in=SCOPED_MODULES).update(can_view_all=False)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0008_rolepermission_can_view_all"),
    ]

    operations = [
        migrations.RunPython(apply, unapply),
    ]
