"""
Seed the dynamic RBAC system and backfill existing users.

Idempotent: safe to re-run. It
  1. populates the Module catalog,
  2. creates the default Roles with a sensible permission matrix
     (only filling matrices for roles that have NO permission rows yet, so
     admin edits made in the UI are never overwritten),
  3. maps existing users' deprecated `role` CharField to a primary_role.

The same logic also runs automatically as the 0003_seed_rbac data migration
(see accounts/rbac_seed.py) so fresh databases are always seeded; this command
exists for re-running after edits or on legacy databases.

Run:  python manage.py seed_rbac
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Module, Role, RolePermission, User
from accounts.rbac_seed import seed_all


class Command(BaseCommand):
    help = "Seed RBAC modules, default roles + matrices; backfill users' roles."

    @transaction.atomic
    def handle(self, *args, **options):
        mapped = seed_all(Module, Role, RolePermission, User,
                          log=self.stdout.write)
        self.stdout.write(self.style.SUCCESS(
            f"RBAC seeded: {Module.objects.count()} modules, "
            f"{Role.objects.count()} roles; {mapped} user(s) mapped."))
