"""Backfill the read permissions that are now actually enforced.

Until this release, no view checked `clients.view`, `projects.view` or
`documents.view`, and PDF export checked nothing at all — the whole read
surface was open to anyone logged in, so a role could hold `edit` on a module
without holding `view` and nobody noticed. Those checks exist now, which turns
every such gap into a locked door.

Two additive passes, neither of which ever removes a permission:

1. Coherence: any role with ANY action on a module also gets `view`. You
   cannot meaningfully edit, approve or export what you cannot open.
2. Design intent: for the seeded roles, restore the specific (module, action)
   pairs this release started enforcing, where the documented matrix grants
   them. This is what gives the delivery roles their newly-required
   `clients.view` and `documents.export` (see rbac_seed._DELIVERY), which pass
   1 can't infer — they hold no clients row at all, and export isn't implied
   by anything.

Deliberately NOT a re-seed: only the pairs below are considered, so an
admin-customised matrix keeps every choice it made and only regains access the
design always assumed it had.
"""
from django.db import migrations

from accounts.rbac_seed import ACTIONS, MATRICES

# The checks this release added. Backfilling anything else would be a re-seed.
NEWLY_ENFORCED = [
    ("clients", "view"),
    ("projects", "view"),
    ("documents", "view"),
    ("documents", "export"),
]


def backfill(apps, schema_editor):
    Module = apps.get_model("accounts", "Module")
    Role = apps.get_model("accounts", "Role")
    RolePermission = apps.get_model("accounts", "RolePermission")

    # Pass 1 — any granted action implies view.
    implied = []
    for rp in RolePermission.objects.filter(can_view=False):
        if any(getattr(rp, f"can_{a}") for a in ACTIONS if a != "view"):
            rp.can_view = True
            implied.append(rp)
    if implied:
        RolePermission.objects.bulk_update(implied, ["can_view"])

    # Pass 2 — seeded roles regain exactly the pairs this release enforces.
    modules = {m.key: m for m in Module.objects.all()}
    for role in Role.objects.filter(name__in=MATRICES):
        matrix = MATRICES[role.name]
        existing = {rp.module_id: rp
                    for rp in RolePermission.objects.filter(role=role)}
        created, updated = {}, set()
        for key, action in NEWLY_ENFORCED:
            module = modules.get(key)
            if module is None or action not in matrix.get(key, ()):
                continue
            rp = existing.get(module.pk) or created.get(module.pk)
            if rp is None:
                rp = RolePermission(role=role, module=module)
                created[module.pk] = rp
            if not getattr(rp, f"can_{action}"):
                setattr(rp, f"can_{action}", True)
                if rp.pk:
                    updated.add(rp)
        if created:
            RolePermission.objects.bulk_create(created.values())
        for rp in updated:
            rp.save()


def noop(apps, schema_editor):
    """Irreversible by design: we can't tell a backfilled `view` from one an
    admin set deliberately, and revoking read access on a downgrade would be
    worse than leaving it."""


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0005_alter_user_role"),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
