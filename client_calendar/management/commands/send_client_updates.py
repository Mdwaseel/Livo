"""Email clients what changed on their content calendar.

Thin, like `send_calendar_reminders`: everything lives in
`client_calendar.updates.run`. Run it every ten minutes — "as things change"
emails wait for a quiet spell after the last edit, and daily/weekly summaries
go on the first run after the morning digest hour. Repeat and overlapping runs
are safe; each person's cursor only moves once their email has gone.

    */10 * * * * cd /srv/agencyos && .venv/bin/python manage.py send_client_updates
"""
from django.core.management.base import BaseCommand

from client_calendar import updates


class Command(BaseCommand):
    help = "Send clients their calendar update emails."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report who is due an email without sending one.")

    def handle(self, *args, **options):
        result = updates.run(dry_run=options["dry_run"])
        if result["site_url_missing"]:
            self.stderr.write(self.style.WARNING(
                "SITE_URL is not set, so update emails carry no links. "
                "Set SITE_URL in .env (e.g. https://za-an.com)."))
        verb = "Would send" if result["dry_run"] else "Sent"
        self.stdout.write(f"{verb} {result['sent']} update email(s) "
                          f"to {result['subscribers']} subscriber(s).")
