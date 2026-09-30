"""Materialise due RecurringTask templates into real Tasks.

Meant to run once a day from cron / Task Scheduler:

    python manage.py generate_recurring_tasks

Idempotent within a day: after a template fires, its next_run is advanced past
today, so a second run the same day is a no-op.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import log_activity, notify
from projects.emails import send_task_assigned
from projects.models import RecurringTask


class Command(BaseCommand):
    help = "Create tasks from recurring templates whose next_run has arrived."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show what would be created without writing anything.")

    def handle(self, *args, **options):
        today = timezone.localdate()
        dry_run = options["dry_run"]

        # Materialised up front: the loop advances next_run, so a lazy
        # queryset would re-evaluate to nothing by the time we report on it.
        due = list(RecurringTask.objects
                   .filter(is_active=True, next_run__lte=today)
                   .select_related("project", "assignee", "reviewer"))

        created = 0
        for template in due:
            if dry_run:
                self.stdout.write(
                    f"  would create “{template.title}” · {template.project.name} "
                    f"(due {template.next_run})")
                created += 1
                continue

            with transaction.atomic():
                task = template.spawn()
                previous = template.next_run
                template.advance()
                template.save(update_fields=["next_run", "updated_at"])

            created += 1
            self.stdout.write(
                f"  + {task.title} · {task.project.name} "
                f"({previous} → {template.next_run})")

            log_activity(None, "generated recurring task",
                         f"{task.title} · {task.project.name}")
            if task.assignee:
                notify([task.assignee],
                       f"Recurring task assigned: {task.title}",
                       url=task.get_absolute_url())
                # No request here, so the link in the email comes from SITE_URL;
                # without it the email still goes, just without a button.
                send_task_assigned(task)

        verb = "Would create" if dry_run else "Created"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {created} task{'' if created == 1 else 's'} "
            f"from {len(due)} due template{'' if len(due) == 1 else 's'}."))
