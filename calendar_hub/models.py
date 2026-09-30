"""Three tables, and a deliberate account of why each one exists.

The brief says not to duplicate existing data, so the test applied to every
field below was: *could the system already answer this?* Where the answer was
yes, nothing was stored — task due dates, milestones, project deadlines, leave,
birthdays and document review deadlines are all read live out of the apps that
own them (see `sources/`). Where the answer was no, a table earned its place:

* `CalendarEvent` — a scheduled meeting or event. Nothing in the codebase holds
  a *future* appointment. `WorkLogEntry.Category.MEETING` is the record of a
  meeting that already happened, logged after the fact with hours attached; it
  cannot express "Tuesday 3pm, client review, these six people". Recurrence
  lives here too, as a rule rather than as rows.

* `EventOccurrence` — an exception to a recurring series: one instance moved to
  another day, or cancelled. Storing it is the only way to say "the stand-up is
  every weekday *except* the 14th" without either materialising thousands of
  rows or losing the exception. It is also exactly how iCalendar, Google and
  Outlook model the same thing (RECURRENCE-ID / EXDATE), which is why sync can
  be added later without reshaping anything.

* `ReminderLog` — proof that a given reminder already went out. Without it the
  nightly command re-notifies everybody about the same deadline every time it
  runs, and a retry after a failure becomes a second round of emails. It keys on
  a *string* rather than a foreign key precisely because most calendar entries
  have no row of their own: "task:57" and "event:12@2026-08-03" both need to be
  nameable.

Two fields were added to other apps rather than here — `EmployeeProfile.
date_of_birth` and `Document.review_due_date`. A birthday is a fact about a
person and a review deadline is a fact about a document; holding either in a
calendar table would be the duplication this module is supposed to avoid.
"""
from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse

from core.models import TimeStampedModel

# ISO weekday digits, Mon=1 … Sun=7 — the same encoding
# `resource_planner.CapacityProfile.working_days` uses, so the two modules agree
# on what "weekdays" means.
WEEKDAY_LABELS = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu",
                  5: "Fri", 6: "Sat", 7: "Sun"}

# How far a recurring series is ever expanded, as a backstop. A rule with no end
# date is legitimate ("stand-up, forever"), but something has to stop the
# expansion, and an accidental DAILY/interval-1 rule queried over a decade would
# otherwise build a hundred thousand objects in memory.
MAX_OCCURRENCES = 750


def _validate_weekdays(value):
    if not value:
        return  # blank means "every day the frequency lands on"
    if any(character not in "1234567" for character in value):
        raise ValidationError(
            "Weekdays must be ISO digits, 1 (Mon) to 7 (Sun).")
    if len(set(value)) != len(value):
        raise ValidationError("Each weekday can only be listed once.")


class CalendarEvent(TimeStampedModel):
    """A meeting or an event somebody scheduled.

    All-day and timed events share one model because they share every other
    field; `start_time` being null *is* the all-day flag. A separate boolean
    would be a second source of truth for the same fact and would eventually
    disagree with the times beside it.
    """

    class Kind(models.TextChoices):
        MEETING = "MEETING", "Meeting"
        EVENT = "EVENT", "Event"

    class Frequency(models.TextChoices):
        NONE = "NONE", "Does not repeat"
        DAILY = "DAILY", "Daily"
        WEEKLY = "WEEKLY", "Weekly"
        MONTHLY = "MONTHLY", "Monthly"
        YEARLY = "YEARLY", "Yearly"

    class Visibility(models.TextChoices):
        # An event everybody with calendar.view can see.
        AGENCY = "AGENCY", "Everyone"
        # Visible only to the organiser and the invitees. A one-to-one about
        # somebody's performance review should not be an agency-wide banner.
        PRIVATE = "PRIVATE", "Organiser and attendees only"

    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.MEETING)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    location = models.CharField(max_length=200, blank=True)
    meeting_url = models.URLField(
        max_length=500, blank=True,
        help_text="Video call link — Meet, Zoom, Teams.")

    start_date = models.DateField()
    end_date = models.DateField(
        null=True, blank=True,
        help_text="Inclusive last day. Leave blank for a single-day event.")
    start_time = models.TimeField(
        null=True, blank=True, help_text="Leave blank for an all-day event.")
    end_time = models.TimeField(null=True, blank=True)

    # --- recurrence, stored as a rule and expanded on read ---
    frequency = models.CharField(max_length=8, choices=Frequency.choices,
                                 default=Frequency.NONE)
    interval = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(52)],
        help_text="Every N days/weeks/months/years.")
    weekdays = models.CharField(
        max_length=7, blank=True, validators=[_validate_weekdays],
        help_text="Weekly only: ISO digits, 1 (Mon) to 7 (Sun). "
                  "Blank repeats on the start date's own weekday.")
    repeat_until = models.DateField(
        null=True, blank=True, help_text="Last day the series can land on.")
    repeat_count = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MaxValueValidator(MAX_OCCURRENCES)],
        help_text="Stop after this many occurrences.")

    # --- tenancy ---
    # Meetings, reviews and internal events belong to whoever's world they sit
    # in. NULL reads as the home workspace. See core.tenancy.
    workspace = models.ForeignKey(
        "core.Workspace", on_delete=models.CASCADE, null=True, blank=True,
        related_name="calendar_events")

    # --- scoping: what the calendar's five filters match on ---
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, null=True, blank=True,
        related_name="calendar_events")
    client = models.ForeignKey(
        "clients.Client", on_delete=models.CASCADE, null=True, blank=True,
        related_name="calendar_events",
        help_text="Filled from the project when one is chosen.")
    department = models.ForeignKey(
        "accounts.Department", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="calendar_events")
    attendees = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="calendar_events")

    visibility = models.CharField(max_length=8, choices=Visibility.choices,
                                  default=Visibility.AGENCY)
    is_cancelled = models.BooleanField(default=False)

    # Minutes of notice. Null means nobody is reminded — distinct from 0, which
    # would mean "tell me as it starts".
    reminder_minutes = models.PositiveIntegerField(
        null=True, blank=True, default=60,
        help_text="Minutes before the start to notify attendees.")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_calendar_events")

    # --- external sync (see sync/base.py) ---
    external_provider = models.CharField(
        max_length=20, blank=True,
        help_text="Set by an external calendar integration; blank for local events.")
    external_id = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["start_date", "start_time", "id"]
        indexes = [
            # The calendar's hot path: "everything that could land in this
            # window". Recurring rows are matched on start_date alone, so the
            # leading column is the one that always narrows.
            models.Index(fields=["start_date", "end_date"]),
            models.Index(fields=["frequency", "start_date"]),
            models.Index(fields=["workspace", "start_date"]),
        ]
        constraints = [
            # An external event must not be imported twice. Local events keep
            # both fields blank and are excluded.
            models.UniqueConstraint(
                fields=["external_provider", "external_id"],
                condition=~models.Q(external_id=""),
                name="unique_external_calendar_event"),
        ]

    def __str__(self):
        return f"{self.title} · {self.start_date}"

    def get_absolute_url(self):
        return reverse("calendar_hub:event_detail", args=[self.pk])

    # --- validation ---

    def clean(self):
        errors = {}
        if self.end_date and self.start_date and self.end_date < self.start_date:
            errors["end_date"] = "The end date can't be before the start."
        if self.end_time and not self.start_time:
            errors["end_time"] = "An end time needs a start time."
        if (self.start_time and self.end_time and not self.spans_days
                and self.end_time <= self.start_time):
            errors["end_time"] = "The end time must be after the start time."
        if self.frequency == self.Frequency.NONE:
            if self.weekdays:
                errors["weekdays"] = "Weekdays only apply to a repeating event."
        elif self.repeat_until and self.repeat_until < self.start_date:
            errors["repeat_until"] = "The series would end before it started."
        if self.weekdays and self.frequency != self.Frequency.WEEKLY:
            errors["weekdays"] = "Weekdays only apply to a weekly repeat."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # A meeting attached to a project is a meeting with that project's
        # client, and making the user say so twice is how the two end up
        # disagreeing.
        if self.project_id and not self.client_id:
            self.client_id = self.project.client_id
        super().save(*args, **kwargs)

    # --- derived ---

    @property
    def spans_days(self):
        return bool(self.end_date and self.end_date > self.start_date)

    @property
    def last_date(self):
        return self.end_date or self.start_date

    @property
    def is_all_day(self):
        return self.start_time is None

    @property
    def is_recurring(self):
        return self.frequency != self.Frequency.NONE

    @property
    def duration_days(self):
        return (self.last_date - self.start_date).days + 1

    @property
    def weekday_numbers(self):
        """The ISO weekdays a weekly series lands on.

        Falls back to the start date's own weekday, which is what a bare
        "repeat weekly" means to anyone who isn't reading the schema.
        """
        if self.weekdays:
            return sorted(int(character) for character in self.weekdays)
        return [self.start_date.isoweekday()]

    @property
    def weekday_label(self):
        return ", ".join(WEEKDAY_LABELS[number]
                         for number in self.weekday_numbers)

    @property
    def recurrence_label(self):
        """Human-readable rule, e.g. "Every 2 weeks on Mon, Thu until 12 Dec"."""
        if not self.is_recurring:
            return ""
        unit = {self.Frequency.DAILY: "day", self.Frequency.WEEKLY: "week",
                self.Frequency.MONTHLY: "month",
                self.Frequency.YEARLY: "year"}[self.frequency]
        every = unit if self.interval == 1 else f"{self.interval} {unit}s"
        label = f"Every {every}"
        if self.frequency == self.Frequency.WEEKLY:
            label += f" on {self.weekday_label}"
        if self.repeat_until:
            label += f" until {self.repeat_until:%d %b %Y}"
        elif self.repeat_count:
            label += f", {self.repeat_count} times"
        return label

    def visible_to(self, user):
        """Private events are for the organiser and the invitees.

        Checked in Python because the queryset does the same filtering in SQL
        (`selectors.visible_events`); this is the belt to that braces, used on
        the detail page where a single object arrives by primary key.
        """
        if self.visibility != self.Visibility.PRIVATE:
            return True
        if not getattr(user, "is_authenticated", False):
            return False
        if self.created_by_id == user.pk:
            return True
        return self.attendees.filter(pk=user.pk).exists()


class EventOccurrence(TimeStampedModel):
    """One instance of a recurring series that differs from the rule.

    Only exceptions are stored. The ordinary occurrences of a daily stand-up are
    computed by `recurrence.expand` and never written down — materialising them
    would be both the duplication the brief rules out and a migration problem
    the first time somebody edits the series.
    """

    event = models.ForeignKey(CalendarEvent, on_delete=models.CASCADE,
                              related_name="occurrences")
    # The date the rule *would* have produced. This is the identity of the
    # instance — iCalendar calls it RECURRENCE-ID — so it never changes, even
    # when the instance is moved.
    original_date = models.DateField()
    # Where it actually happens. Null means "not moved"; combined with
    # is_cancelled=True that is an EXDATE.
    moved_to = models.DateField(null=True, blank=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    is_cancelled = models.BooleanField(default=False)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["original_date"]
        constraints = [
            models.UniqueConstraint(fields=["event", "original_date"],
                                    name="unique_event_occurrence"),
        ]
        indexes = [models.Index(fields=["event", "original_date"])]

    def __str__(self):
        if self.is_cancelled:
            return f"{self.event.title} · {self.original_date} cancelled"
        return f"{self.event.title} · {self.original_date} → {self.moved_to}"

    @property
    def effective_date(self):
        return self.moved_to or self.original_date


class ReminderLog(TimeStampedModel):
    """"This reminder has already been sent." Nothing more.

    The unique constraint is the whole feature: it makes
    `send_calendar_reminders` idempotent, so running it every ten minutes — or
    twice by accident, or again after a crash — notifies each person about each
    thing exactly once.

    `source_key` is a string because most calendar entries are derived and have
    no row here to point a foreign key at. It matches `events.Event.key`.
    """

    class Kind(models.TextChoices):
        REMINDER = "REMINDER", "Event reminder"
        DEADLINE = "DEADLINE", "Upcoming deadline"

    source_key = models.CharField(max_length=120)
    # The day the reminded-about thing happens, so a recurring stand-up is
    # remindable every week rather than once forever.
    event_date = models.DateField()
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="calendar_reminders")
    kind = models.CharField(max_length=10, choices=Kind.choices,
                            default=Kind.REMINDER)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_key", "event_date", "user", "kind"],
                name="unique_reminder_delivery"),
        ]
        indexes = [models.Index(fields=["event_date"])]

    def __str__(self):
        return f"{self.get_kind_display()} · {self.source_key} → {self.user}"

    @classmethod
    def purge_before(cls, cutoff=None):
        """Drop ledger rows for things that happened well in the past.

        The ledger only has to remember long enough to stop a duplicate send;
        keeping it forever turns an idempotency guard into an ever-growing
        table.
        """
        cutoff = cutoff or (date.today() - timedelta(days=90))
        return cls.objects.filter(event_date__lt=cutoff).delete()
