"""Create or update a full-access owner account.

    python manage.py ensure_owner hsn --password '...'
    LIVO_OWNER_PASSWORD='...' python manage.py ensure_owner hsn

Idempotent, so it is safe to re-run against a fresh production database right
after `migrate` (see VERCEL.md). Access comes from the Super Admin primary role;
the legacy `role` field is set to OWNER only so older screens label it right.
The workspace is left unset, which reads as the home workspace.
"""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from accounts.models import Role


class Command(BaseCommand):
    help = "Create or update a superuser with the Super Admin role."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument(
            "--password", default=os.getenv("LIVO_OWNER_PASSWORD", ""),
            help="Defaults to the LIVO_OWNER_PASSWORD environment variable.")
        parser.add_argument("--email", default="")

    def handle(self, *args, username, password, email, **options):
        if not password:
            raise CommandError("Pass --password or set LIVO_OWNER_PASSWORD.")
        super_admin = Role.objects.filter(name="Super Admin").first()
        if not super_admin:
            raise CommandError("No 'Super Admin' role — run `migrate` first.")

        User = get_user_model()
        user, created = User.objects.get_or_create(username=username)
        user.set_password(password)
        user.is_superuser = user.is_staff = user.is_active = True
        user.role = User.Role.OWNER
        user.primary_role = super_admin
        if email:
            user.email = email
        user.save()
        self.stdout.write(self.style.SUCCESS(
            f"{'Created' if created else 'Updated'} owner '{username}'."))
