"""Nightly (or ten-minutely) reminder dispatch.

Deliberately thin: everything it does lives in `calendar_hub.reminders.run`, so
the behaviour is testable without a scheduler and the command is only argument
parsing and output. `projects.generate_recurring_tasks` is the existing command
this follows.

Cadence: every 10–15 minutes is right. `ReminderLog` makes repeat runs free, and
a meeting reminder set to "60 minutes before" can only be as punctual as the
scheduler that fires it. A once-a-day run still works — deadline alerts land
correctly and meeting reminders are simply as late as the gap allows.

    */10 * * * * cd /srv/livo && .venv/bin/python manage.py send_calendar_reminders
"""
from django.core.management.base import BaseCommand

from calendar_hub import reminders
from calendar_hub.models import ReminderLog


class Command(BaseCommand):
    help = "Send calendar reminders and upcoming-deadline alerts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be sent without sending or logging it.")
        parser.add_argument(
            "--purge", action="store_true",
            help="Also drop ledger rows for dates more than 90 days past.")

    def handle(self, *args, **options):
        result = reminders.run(dry_run=options["dry_run"])
        prefix = "Would send" if result["dry_run"] else "Sent"
        self.stdout.write(
            f"{prefix} {result['sent'] or result['reminders'] + result['alerts']} "
            f"notification(s): {result['reminders']} meeting reminder(s), "
            f"{result['alerts']} deadline alert(s).")

        if options["purge"]:
            removed, _ = ReminderLog.purge_before()
            self.stdout.write(f"Purged {removed} old ledger row(s).")

        if not result["dry_run"]:
            self.stdout.write(self.style.SUCCESS("Done."))
