from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone

from core.models import TimeStampedModel
from clients.models import Client


# Default onboarding checklist for every new project.
# (key, label, is_auto) — edit here to change the default set.
# Auto items are managed by code (sync_onboarding) and can't be hand-toggled.
DEFAULT_ONBOARDING = [
    ("contract_signed",  "Contract signed",          False),
    ("advance_received", "Advance received",         True),
    ("assets_received",  "Brand assets received",    False),
    ("access_received",  "Logins / access received", False),
    ("kickoff_done",     "Kickoff meeting held",     False),
    ("env_setup",        "Project & repo set up",    False),
]

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif")


class Project(TimeStampedModel):
    """An engagement for a client. Documents live under a project."""

    class Status(models.TextChoices):
        LEAD = "LEAD", "Lead"
        PROPOSAL = "PROPOSAL", "Proposal Sent"
        ACTIVE = "ACTIVE", "Active"
        ON_HOLD = "ON_HOLD", "On Hold"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="projects")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    project_type = models.CharField(max_length=120, blank=True,
                                    help_text="e.g. Website, E-commerce, Mobile App")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.LEAD)

    budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="INR")
    start_date = models.DateField(null=True, blank=True)
    target_end_date = models.DateField(null=True, blank=True)

    # Quick links for the delivery workspace Overview.
    live_url = models.URLField(blank=True)
    staging_url = models.URLField(blank=True)
    repo_url = models.URLField(blank=True)
    assets_url = models.URLField(blank=True, help_text="Drive/Dropbox folder etc.")

    is_default = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    # Who is on this project. Membership is what makes a project visible at all
    # to anyone without the projects.view_all bypass — see projects/access.py.
    # Deliberately a plain M2M rather than a through model with a per-project
    # role: the RBAC matrix already decides what someone may DO, and duplicating
    # that per project would give two sources of truth for one question.
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="member_projects",
        help_text="People who can see this project. Managers see every project "
                  "regardless of this list.",
    )

    # --- tenancy ---
    # Which partition this record belongs to. NULL reads as the home
    # workspace, so rows written before partner access shipped stay ours.
    # See core.tenancy for how it reaches every query.
    workspace = models.ForeignKey(
        "core.Workspace", on_delete=models.CASCADE, null=True, blank=True,
        related_name="projects",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["workspace", "is_archived"])]

    def __str__(self):
        return f"{self.name} · {self.client.name}"

    def get_absolute_url(self):
        return reverse("projects:detail", args=[self.pk])

    # --- finance rollups (single-currency INR assumption for now) ---
    # `budget` is the agreed project value; UI labels it "Project value".

    @property
    def total_received(self):
        return self.payments.aggregate(s=models.Sum("amount"))["s"] or Decimal("0")

    @property
    def outstanding(self):
        return (self.budget or Decimal("0")) - self.total_received

    @property
    def payment_status(self):
        received = self.total_received
        value = self.budget or Decimal("0")
        if received == 0:
            return "Unpaid"
        if received < value:
            return "Partially paid"
        if received == value:
            return "Paid in full"
        return "Overpaid"

    # --- onboarding checklist ---

    def ensure_onboarding(self):
        """Idempotently create any missing default checklist items.

        Called from the post-create signal and from the detail view, so
        projects that predate this feature grow a checklist on first visit.
        """
        existing = set(self.onboarding_items.values_list("key", flat=True))
        missing = [
            OnboardingItem(project=self, key=key, label=label,
                           is_auto=is_auto, order=idx)
            for idx, (key, label, is_auto) in enumerate(DEFAULT_ONBOARDING)
            if key not in existing
        ]
        if missing:
            OnboardingItem.objects.bulk_create(missing)

    def sync_onboarding(self):
        """Auto-tick 'Advance received' from finance data: any ADVANCE
        payment, or any money received at all, counts."""
        item = self.onboarding_items.filter(key="advance_received").first()
        if item is None:
            return
        from finance.models import Payment
        should_be_done = (
            self.payments.filter(payment_type=Payment.Type.ADVANCE).exists()
            or self.total_received > 0
        )
        if should_be_done and not item.is_done:
            item.is_done = True
            item.completed_on = timezone.localdate()
            item.save(update_fields=["is_done", "completed_on", "updated_at"])
        elif not should_be_done and item.is_done:
            item.is_done = False
            item.completed_on = None
            item.completed_by = None
            item.save(update_fields=["is_done", "completed_on", "completed_by",
                                     "updated_at"])

    @property
    def onboarding_progress(self):
        """{done, total, percent} — uses the prefetch cache when present."""
        items = list(self.onboarding_items.all())
        total = len(items)
        done = sum(1 for i in items if i.is_done)
        percent = round(done * 100 / total) if total else 0
        return {"done": done, "total": total, "percent": percent}

    # --- delivery workspace helpers ---

    @property
    def task_summary(self):
        """{todo, in_progress, submitted, done, total} — uses the prefetch cache.

        `submitted` is carved out of in-progress work: a task sitting with the
        reviewer is neither still being worked on nor finished.
        """
        tasks = list(self.tasks.all())
        summary = {"todo": 0, "in_progress": 0, "submitted": 0,
                   "done": 0, "total": len(tasks)}
        for t in tasks:
            summary[t.board_column] += 1
        return summary

    def worklog_this_month(self):
        """Entries for the current month — what the monthly report reads."""
        today = timezone.localdate()
        return self.work_logs.filter(date__year=today.year, date__month=today.month)


class OnboardingItem(TimeStampedModel):
    """One step of a project's onboarding checklist."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE,
                                related_name="onboarding_items")
    key = models.SlugField(max_length=60)
    label = models.CharField(max_length=200)
    is_done = models.BooleanField(default=False)
    is_auto = models.BooleanField(
        default=False, help_text="Auto-managed by code; can't be toggled by hand")
    completed_on = models.DateField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        unique_together = ("project", "key")

    def __str__(self):
        return f"{self.label} · {self.project.name}"


class Sprint(TimeStampedModel):
    """A time-boxed batch of tasks inside a project."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE,
                                related_name="sprints")
    name = models.CharField(max_length=120)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-start_date", "-id"]

    def __str__(self):
        return f"{self.name} · {self.project.name}"


class Task(TimeStampedModel):
    """A unit of delivery work inside a project.

    Two axes, deliberately kept separate:

    * `status`         — where the work is (TODO / IN_PROGRESS / DONE).
    * `approval_status`— where the *review* is (NONE / SUBMITTED / APPROVED /
                         REOPENED).

    A task with a reviewer never jumps straight to DONE: finishing it moves
    approval_status to SUBMITTED while status stays IN_PROGRESS, and only the
    reviewer's approval writes DONE. Tasks without a reviewer skip the whole
    dance and complete directly. See `board_column` for how the two axes
    collapse into the four UI columns.
    """

    class Status(models.TextChoices):
        TODO = "TODO", "To do"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        DONE = "DONE", "Done"

    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        MED = "MED", "Medium"
        HIGH = "HIGH", "High"

    class Approval(models.TextChoices):
        NONE = "NONE", "Not submitted"
        SUBMITTED = "SUBMITTED", "Awaiting review"
        APPROVED = "APPROVED", "Approved"
        REOPENED = "REOPENED", "Reopened"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.TODO)
    priority = models.CharField(max_length=6, choices=Priority.choices,
                                default=Priority.MED)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assigned_tasks",
    )
    due_date = models.DateField(null=True, blank=True)
    completed_on = models.DateField(null=True, blank=True)
    order = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    # --- org / planning ---
    department = models.ForeignKey(
        "accounts.Department", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tasks",
    )
    sprint = models.ForeignKey(
        Sprint, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tasks",
    )

    # --- effort ---
    estimated_hours = models.DecimalField(max_digits=6, decimal_places=2,
                                          null=True, blank=True)
    actual_hours = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("0"),
        help_text="Rolled up from work logs.")
    is_billable = models.BooleanField(default=True)

    # --- review / approval ---
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="review_tasks",
    )
    approval_status = models.CharField(max_length=10, choices=Approval.choices,
                                       default=Approval.NONE)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="approved_tasks",
    )
    approved_on = models.DateTimeField(null=True, blank=True)

    # --- dependencies ---
    blocked_by = models.ManyToManyField(
        "self", symmetrical=False, blank=True, related_name="blocks",
        help_text="Tasks that must be done before this one can complete.",
    )

    class Meta:
        ordering = ["order", "-created_at"]

    def __str__(self):
        return f"{self.title} · {self.project.name}"

    def get_absolute_url(self):
        return reverse("projects:task_detail", args=[self.pk])

    # --- derived state ---

    @property
    def board_column(self):
        """Which of the four board columns this task belongs in.

        Matches the keys of Project.task_summary, so the two never drift.
        """
        if self.status == self.Status.DONE:
            return "done"
        if self.approval_status == self.Approval.SUBMITTED:
            return "submitted"
        if self.status == self.Status.IN_PROGRESS:
            return "in_progress"
        return "todo"

    @property
    def needs_review(self):
        """A reviewer is set, so completion has to go through them."""
        return self.reviewer_id is not None

    @property
    def is_awaiting_review(self):
        return self.approval_status == self.Approval.SUBMITTED

    @property
    def open_blockers(self):
        """Blockers that aren't finished yet — the list that stops completion."""
        return [t for t in self.blocked_by.all() if t.status != self.Status.DONE]

    @property
    def is_blocked(self):
        return bool(self.open_blockers)

    @property
    def checklist_progress(self):
        """{done, total, percent} — uses the prefetch cache when present."""
        items = list(self.checklist.all())
        total = len(items)
        done = sum(1 for i in items if i.is_done)
        return {"done": done, "total": total,
                "percent": round(done * 100 / total) if total else 0}

    @property
    def is_overdue(self):
        return bool(
            self.due_date
            and self.status != self.Status.DONE
            and self.due_date < timezone.localdate()
        )

    # --- effort rollup ---

    def sync_actual_hours(self):
        """Recompute actual_hours from this task's work logs.

        Counts *all* logged hours, not only approved ones, so effort shows up
        the moment it's logged rather than after a review round-trip. This is
        the only writer of actual_hours — it is never entered by hand.
        """
        total = self.work_logs.aggregate(s=models.Sum("hours"))["s"] or Decimal("0")
        if total != self.actual_hours:
            self.actual_hours = total
            Task.objects.filter(pk=self.pk).update(actual_hours=total)
        return total

    @property
    def hours_variance(self):
        """actual − estimated, or None when there's nothing to compare."""
        if self.estimated_hours is None:
            return None
        return self.actual_hours - self.estimated_hours

    @property
    def is_over_estimate(self):
        variance = self.hours_variance
        return variance is not None and variance > 0

    @property
    def effort_percent(self):
        """actual as a % of estimate, capped at 100 for the progress bar.

        `is_over_estimate` carries the overrun signal — the bar just fills.
        """
        if not self.estimated_hours:
            return 0
        return min(100, round(self.actual_hours * 100 / self.estimated_hours))


class TaskChecklistItem(TimeStampedModel):
    """One tick-box inside a task."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="checklist")
    text = models.CharField(max_length=250)
    is_done = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.text} · {self.task.title}"


class TaskComment(TimeStampedModel):
    """A message on a task's thread. Review decisions post here too."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="task_comments",
    )
    body = models.TextField()

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.author} on {self.task.title}"


class TaskAttachment(TimeStampedModel):
    """A file hanging off a task, or a link to one held elsewhere.

    Exactly one of `file` / `external_url` is set. Anything over the upload
    ceiling is kept out of the VPS's media directory and referenced by link
    instead — see core.uploads.
    """

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="task_attachments/", blank=True)
    external_url = models.URLField(
        max_length=500, blank=True,
        help_text="Google Drive (or similar) link, used for files over the size limit")
    label = models.CharField(max_length=200, blank=True,
                             help_text="Shown instead of the URL for linked files")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="task_attachments",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.filename} · {self.task.title}"

    @property
    def is_link(self):
        return not self.file and bool(self.external_url)

    @property
    def url(self):
        """Where to point an <a href> — a link record has no stored file."""
        return self.external_url if self.is_link else (self.file.url if self.file else "")

    @property
    def filename(self):
        if self.is_link:
            return self.label or self.external_url
        return self.file.name.rsplit("/", 1)[-1] if self.file else ""

    @property
    def is_image(self):
        return bool(self.file) and self.file.name.lower().endswith(IMAGE_EXTENSIONS)


class RecurringTask(TimeStampedModel):
    """A template that spawns a real Task on a schedule.

    `generate_recurring_tasks` (run daily by cron) materialises every active
    template whose next_run has arrived, then advances next_run.
    """

    class Frequency(models.TextChoices):
        DAILY = "DAILY", "Daily"
        WEEKLY = "WEEKLY", "Weekly"
        MONTHLY = "MONTHLY", "Monthly"

    project = models.ForeignKey(Project, on_delete=models.CASCADE,
                                related_name="recurring_tasks")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    department = models.ForeignKey(
        "accounts.Department", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="recurring_tasks",
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="recurring_tasks",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="recurring_review_tasks",
    )
    priority = models.CharField(max_length=6, choices=Task.Priority.choices,
                                default=Task.Priority.MED)
    estimated_hours = models.DecimalField(max_digits=6, decimal_places=2,
                                          null=True, blank=True)
    frequency = models.CharField(max_length=8, choices=Frequency.choices,
                                 default=Frequency.WEEKLY)
    next_run = models.DateField(default=timezone.localdate)
    # The day-of-month a MONTHLY template is anchored to, captured from the
    # first next_run. Without it, clamping is permanent and drifts: a 31st
    # template hits Feb 28, then keeps stepping from 28 forever.
    anchor_day = models.PositiveSmallIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["next_run", "title"]

    def __str__(self):
        return f"{self.title} ({self.get_frequency_display()}) · {self.project.name}"

    def save(self, *args, **kwargs):
        if self.anchor_day is None and self.next_run:
            self.anchor_day = self.next_run.day
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "anchor_day" not in update_fields:
                kwargs["update_fields"] = list(update_fields) + ["anchor_day"]
        super().save(*args, **kwargs)

    def advance(self):
        """Push next_run forward by one interval.

        Loops rather than adding once so a template that missed a few days
        (scheduler down, server off) lands in the future instead of firing
        again on the next run.
        """
        today = timezone.localdate()
        while self.next_run <= today:
            self.next_run = self._step(self.next_run)
        return self.next_run

    def _step(self, date):
        if self.frequency == self.Frequency.DAILY:
            return date + timedelta(days=1)
        if self.frequency == self.Frequency.WEEKLY:
            return date + timedelta(days=7)
        # Monthly: the anchor day next month, clamped to the month's length so
        # the 31st doesn't skip February. Clamping is applied to the ANCHOR,
        # not to last month's clamped result, so a 31st template goes
        # 31 Jan -> 28 Feb -> 31 Mar rather than sticking at the 28th.
        import calendar
        year, month = date.year, date.month + 1
        if month > 12:
            year, month = year + 1, 1
        day = self.anchor_day or date.day
        return date.replace(year=year, month=month,
                            day=min(day, calendar.monthrange(year, month)[1]))

    def spawn(self):
        """Create the real Task for this cycle."""
        return Task.objects.create(
            project=self.project,
            title=self.title,
            description=self.description,
            department=self.department,
            assignee=self.assignee,
            reviewer=self.reviewer,
            priority=self.priority,
            estimated_hours=self.estimated_hours,
            due_date=self.next_run,
        )


class WorkLogEntry(TimeStampedModel):
    """A dated record of work done — the spine of the monthly report, and the
    single source of truth for logged time.

    An entry may hang off a Task, but doesn't have to: internal meetings and
    admin work belong to a project without belonging to any task, which is why
    `category` survives alongside the task link.

    Hours can be entered directly or derived from start/end times. Whatever
    lands in `hours` is what rolls up into Task.actual_hours.
    """

    class Category(models.TextChoices):
        DESIGN = "DESIGN", "Design"
        DEV = "DEV", "Development"
        CONTENT = "CONTENT", "Content"
        MEETING = "MEETING", "Meeting"
        QA = "QA", "QA / Testing"
        OTHER = "OTHER", "Other"

    class Approval(models.TextChoices):
        NONE = "NONE", "Draft"
        SUBMITTED = "SUBMITTED", "Awaiting approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    project = models.ForeignKey(Project, on_delete=models.CASCADE,
                                related_name="work_logs")
    task = models.ForeignKey(
        "Task", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="work_logs",
        help_text="Optional — leave blank for project-level work.",
    )
    date = models.DateField()
    description = models.TextField()
    category = models.CharField(max_length=8, choices=Category.choices,
                                default=Category.DEV)

    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    hours = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True,
                                help_text="Auto-filled from start/end when blank.")
    is_billable = models.BooleanField(default=True)
    screenshot = models.ImageField(upload_to="worklog_screenshots/",
                                   null=True, blank=True)

    logged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    approval_status = models.CharField(max_length=10, choices=Approval.choices,
                                       default=Approval.NONE)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="approved_work_logs",
    )
    approved_on = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    # Set from the DB row so save() can also refresh the task an entry was
    # moved *away* from. None on unsaved instances.
    _initial_task_id = None

    class Meta:
        ordering = ["-date", "-created_at"]

    def __str__(self):
        return f"{self.date} · {self.get_category_display()} · {self.project.name}"

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        if "task_id" in field_names:
            instance._initial_task_id = instance.task_id
        return instance

    # --- hours ---

    def _as_time(self, field_name):
        """Read a time field as a `time`, even when it holds a raw POST string.

        Django coerces field values on the DB round-trip, not on assignment, so
        an unsaved instance built from form data still carries "09:30" here.
        """
        value = getattr(self, field_name)
        if not value:
            return None
        if isinstance(value, str):
            try:
                return self._meta.get_field(field_name).to_python(value)
            except ValidationError:
                return None
        return value

    def duration_hours(self):
        """Hours between start and end, or None if either is missing.

        An end before the start is read as crossing midnight rather than as a
        negative shift — late sessions are the realistic case here.
        """
        start_time = self._as_time("start_time")
        end_time = self._as_time("end_time")
        if not (start_time and end_time):
            return None
        start = timedelta(hours=start_time.hour,
                          minutes=start_time.minute,
                          seconds=start_time.second)
        end = timedelta(hours=end_time.hour,
                        minutes=end_time.minute,
                        seconds=end_time.second)
        if end < start:
            end += timedelta(days=1)
        seconds = (end - start).total_seconds()
        return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"))

    def save(self, *args, **kwargs):
        if self.hours is None:
            derived = self.duration_hours()
            if derived is not None:
                self.hours = derived
                update_fields = kwargs.get("update_fields")
                if update_fields is not None and "hours" not in update_fields:
                    kwargs["update_fields"] = list(update_fields) + ["hours"]
        super().save(*args, **kwargs)
        self._resync_tasks()

    def delete(self, *args, **kwargs):
        # Grab the task before the row goes away, then resync it afterwards.
        task = self.task
        result = super().delete(*args, **kwargs)
        if task is not None:
            task.sync_actual_hours()
        return result

    def _resync_tasks(self):
        """Refresh the current task and, if the entry moved, the previous one."""
        task_ids = {self._initial_task_id, self.task_id} - {None}
        for task in Task.objects.filter(pk__in=task_ids):
            task.sync_actual_hours()
        self._initial_task_id = self.task_id

    # --- approval ---

    @property
    def is_locked(self):
        """Approved time is settled — editing it reopens the approval."""
        return self.approval_status == self.Approval.APPROVED

    @property
    def is_awaiting_approval(self):
        return self.approval_status == self.Approval.SUBMITTED

    # --- rollups ---

    @classmethod
    def user_month_summary(cls, user, year, month):
        """{total, billable, non_billable, approved, entries} for one person.

        Feeds the timesheet header today and the Performance module later.
        """
        rows = cls.objects.filter(logged_by=user, date__year=year,
                                  date__month=month)
        return cls._summarise(rows)

    @classmethod
    def project_summary(cls, project):
        """{total, billable, non_billable, approved, entries} for one project.

        Feeds billing: `billable` is the number that becomes an invoice line.
        """
        return cls._summarise(cls.objects.filter(project=project))

    @classmethod
    def _summarise(cls, queryset):
        totals = queryset.aggregate(
            total=models.Sum("hours"),
            billable=models.Sum("hours", filter=models.Q(is_billable=True)),
            approved=models.Sum(
                "hours", filter=models.Q(approval_status=cls.Approval.APPROVED)),
            entries=models.Count("id"),
        )
        total = totals["total"] or Decimal("0")
        billable = totals["billable"] or Decimal("0")
        return {
            "total": total,
            "billable": billable,
            "non_billable": total - billable,
            "approved": totals["approved"] or Decimal("0"),
            "entries": totals["entries"],
            "billable_percent": (round(billable * 100 / total) if total else 0),
        }


class Asset(TimeStampedModel):
    """A file attached to a project: brand assets, deliverables, proof shots."""

    class Category(models.TextChoices):
        BRAND = "BRAND", "Brand asset"
        DESIGN = "DESIGN", "Design"
        DELIVERABLE = "DELIVERABLE", "Deliverable"
        SCREENSHOT = "SCREENSHOT", "Screenshot"
        PROOF = "PROOF", "Proof of work"
        CREDENTIAL = "CREDENTIAL", "Credential"
        OTHER = "OTHER", "Other"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="assets")
    title = models.CharField(max_length=200)
    # Either a stored file or, for anything over the upload ceiling, a link to
    # where it lives instead. See core.uploads for the ceiling and why.
    file = models.FileField(upload_to="project_assets/", blank=True)
    external_url = models.URLField(
        max_length=500, blank=True,
        help_text="Google Drive (or similar) link, used for files over the size limit")
    category = models.CharField(max_length=12, choices=Category.choices,
                                default=Category.OTHER)
    is_report_proof = models.BooleanField(
        default=False, help_text="Feature this image in monthly reports")
    notes = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} · {self.project.name}"

    @property
    def is_link(self):
        return not self.file and bool(self.external_url)

    @property
    def url(self):
        return self.external_url if self.is_link else (self.file.url if self.file else "")

    @property
    def is_image(self):
        return bool(self.file) and self.file.name.lower().endswith(IMAGE_EXTENSIONS)


class Milestone(TimeStampedModel):
    """A scheduled checkpoint within a project."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        DONE = "DONE", "Done"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="milestones")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    # When it actually landed. Monthly reports read THIS, not updated_at —
    # touching an old milestone's text used to make it resurface as "reached"
    # in the current month. Written by apply_status(), never by hand.
    completed_on = models.DateField(null=True, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "due_date"]

    def __str__(self):
        return self.title

    def apply_status(self, status):
        """Set the status and keep completed_on in step. Returns the fields to
        save so callers can stay on update_fields."""
        self.status = status
        self._sync_completed_on()
        return ["status", "completed_on", "updated_at"]

    def _sync_completed_on(self):
        """completed_on is exactly 'DONE since'. Returns True if it changed."""
        before = self.completed_on
        if self.status == self.Status.DONE:
            if self.completed_on is None:
                self.completed_on = timezone.localdate()
        else:
            self.completed_on = None
        return self.completed_on != before

    def save(self, *args, **kwargs):
        # Enforced here, not only in apply_status: anything that writes DONE —
        # a fixture, the admin, a bulk import — must get a completion date, or
        # the milestone silently disappears from monthly reports.
        if self._sync_completed_on():
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "completed_on" not in update_fields:
                kwargs["update_fields"] = list(update_fields) + ["completed_on"]
        super().save(*args, **kwargs)
