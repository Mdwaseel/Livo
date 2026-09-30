"""Build demo/livo_demo.sqlite3, the database the Vercel demo runs on.

    python manage.py build_demo_db --password '...'

Starts from an empty file, never from db.sqlite3: the local database holds real
people's emails, salaries and the agency's contact details, and anything in this
file ships inside a public deployment. Contents: every migration, the standard
document types, and one owner account.

Each step runs as a child `manage.py` with DATABASE_URL pointed at the new file,
so this process's own connection (the local database) is never touched.
"""
import os
import subprocess
import sys

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Build the SQLite database bundled with the Vercel demo."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="hsn")
        parser.add_argument(
            "--password", default=os.getenv("LIVO_OWNER_PASSWORD", ""),
            help="Owner password. Defaults to the LIVO_OWNER_PASSWORD environment variable.")

    def handle(self, *args, username, password, **options):
        if not password:
            raise CommandError("Pass --password or set LIVO_OWNER_PASSWORD.")

        target = settings.DEMO_DB_SOURCE
        target.parent.mkdir(exist_ok=True)
        if target.exists():
            target.unlink()

        env = {**os.environ, "DATABASE_URL": f"sqlite:///{target.as_posix()}"}
        env.pop("VERCEL", None)
        manage = str(settings.BASE_DIR / "manage.py")
        steps = [
            ["migrate", "--noinput"],
            ["seed_doctypes"],
            ["ensure_owner", username, "--password", password],
        ]
        for step in steps:
            self.stdout.write(f"· {step[0]}")
            result = subprocess.run([sys.executable, manage, *step], env=env,
                                    capture_output=True, text=True)
            if result.returncode:
                raise CommandError(f"{step[0]} failed:\n{result.stderr[-2000:]}")

        size_kb = target.stat().st_size // 1024
        self.stdout.write(self.style.SUCCESS(
            f"Built {target.relative_to(settings.BASE_DIR)} ({size_kb} KB) "
            f"with owner '{username}'."))
