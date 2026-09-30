"""Two tables, both holding facts that exist nowhere else in the system.

Everything the planner *derives* — allocated hours, remaining capacity,
utilisation, sprint load — comes from `projects.Task` and
`projects.WorkLogEntry` at query time. See `selectors.py`. What is stored here
is only what the codebase could not already answer:

* `CapacityProfile` — how many hours a person is available for, and on which
  weekdays. Nothing in `employees.EmployeeProfile` says this; `date_of_joining`
  and `status` describe employment, not availability.
* `LeaveRecord` — when somebody is away. `EmployeeProfile.Status.ON_LEAVE` is a
  single current-state flag with no dates, so it cannot answer "is Anna free on
  the 14th" or "what is the team's capacity next month". The `leaves` RBAC
  module key has been reserved in the catalog since the RBAC rewrite with no
  model behind it; this fills it.

Neither table duplicates anything. Deleting both would lose real information,
which is the test for whether a table has earned its place.
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel

# The agency default, and the answer for anyone without a saved profile.
DEFAULT_DAILY_HOURS = Decimal("8")
# ISO weekday numbers (Mon=1 … Sun=7). Monday–Friday.
DEFAULT_WORKING_DAYS = "12345"
# Weeks in an average month (52 / 12), used to derive monthly capacity when it
# has not been overridden. 4 would under-count by roughly two days a month.
WEEKS_PER_MONTH = Decimal("4.345")


def _validate_working_days(value):
    if not value:
        raise ValidationError("Pick at least one working day.")
    if any(character not in "1234567" for character in value):
        raise ValidationError("Working days must be ISO weekday digits, 1 (Mon) to 7 (Sun).")
    if len(set(value)) != len(value):
        raise ValidationError("Each working day can only be listed once.")


class CapacityProfile(TimeStampedModel):
    """How much time one person is available for.

    Weekly and monthly figures are *derived* from the daily hours and the
    working-day pattern unless explicitly overridden. Storing all three as
    independent numbers was rejected: they would drift the first time somebody
    changed the daily figure and forgot the other two, and the planner would
    then quietly report a capacity nobody has.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="capacity_profile")
    daily_hours = models.DecimalField(
        max_digits=4, decimal_places=2, default=DEFAULT_DAILY_HOURS,
        help_text="Hours available on a normal working day.")
    working_days = models.CharField(
        max_length=7, default=DEFAULT_WORKING_DAYS,
        validators=[_validate_working_days],
        help_text="ISO weekday digits, 1 (Mon) to 7 (Sun). Default 12345.")
    weekly_hours_override = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Leave blank to derive from daily hours × working days.")
    monthly_hours_override = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Leave blank to derive from the weekly figure.")
    notes = models.CharField(max_length=200, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")

    class Meta:
        ordering = ["user__first_name", "user__username"]
        verbose_name = "capacity profile"

    def __str__(self):
        return f"{self.user} · {self.daily_hours} h/day"

    # --- derived figures ---

    @property
    def working_day_numbers(self):
        return {int(character) for character in self.working_days}

    @property
    def days_per_week(self):
        return len(self.working_day_numbers)

    @property
    def weekly_hours(self):
        if self.weekly_hours_override is not None:
            return self.weekly_hours_override
        return self.daily_hours * self.days_per_week

    @property
    def monthly_hours(self):
        if self.monthly_hours_override is not None:
            return self.monthly_hours_override
        return (self.weekly_hours * WEEKS_PER_MONTH).quantize(Decimal("0.01"))

    def works_on(self, day):
        """`day.isoweekday()` is 1-7 with Monday first, which is why the field
        stores ISO numbers rather than Python's 0-6 `weekday()`."""
        return day.isoweekday() in self.working_day_numbers

    def working_days_between(self, start, end):
        count, cursor = 0, start
        while cursor <= end:
            if self.works_on(cursor):
                count += 1
            cursor += timedelta(days=1)
        return count

    # --- lookup ---

    @classmethod
    def default_for(cls, user):
        """An unsaved profile carrying the agency defaults.

        Returned for anyone without a row so the planner works on day one,
        before a manager has configured a single person. It is deliberately not
        saved: an implicit 8 h/day is a default, and writing it to the database
        would turn it into a decision somebody appears to have made.
        """
        return cls(user=user, daily_hours=DEFAULT_DAILY_HOURS,
                   working_days=DEFAULT_WORKING_DAYS)

    @classmethod
    def map_for(cls, users):
        """{user_id: profile} for a set of users, in one query, with defaults
        filled in for those who have no row."""
        saved = {profile.user_id: profile
                 for profile in cls.objects.filter(user__in=users)}
        return {user.pk: saved.get(user.pk) or cls.default_for(user)
                for user in users}


class LeaveRecord(TimeStampedModel):
    """A period somebody is unavailable.

    A null `user` means a company-wide non-working day (a public holiday, an
    office shutdown) — it reduces everybody's capacity at once, which is both
    less data entry and impossible to get half-right by forgetting a person.
    """

    class Kind(models.TextChoices):
        VACATION = "VACATION", "Earned / vacation"
        CASUAL = "CASUAL", "Casual leave"
        SICK = "SICK", "Sick leave"
        HOLIDAY = "HOLIDAY", "Public holiday"
        COMP_OFF = "COMP_OFF", "Comp off"
        UNPAID = "UNPAID", "Unpaid leave"
        OTHER = "OTHER", "Other"

    # --- how each kind meets the annual allowance (see employees.leave) ---
    # Drawn from the pool. Run out and the excess starts costing salary.
    ALLOWANCE_KINDS = ("VACATION", "CASUAL", "SICK", "OTHER")
    # Free. A public holiday is not somebody's leave to spend, and comp off has
    # already been paid for in the extra hours that earned it.
    FREE_KINDS = ("HOLIDAY", "COMP_OFF")
    # Deducted from day one and never drawn against the pool — that is what
    # makes it *unpaid*. Someone with allowance left who files unpaid leave is
    # choosing to lose the pay, and the pool is still there afterwards.
    UNPAID_KINDS = ("UNPAID",)

    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", "Requested"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="leave_records",
        help_text="Leave blank for a company-wide holiday.")
    kind = models.CharField(max_length=10, choices=Kind.choices,
                            default=Kind.VACATION)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.APPROVED)
    start_date = models.DateField()
    end_date = models.DateField()
    is_half_day = models.BooleanField(
        default=False, help_text="Halves the capacity of every day in the range.")
    # The employee's own words. Required on a request — an approver deciding
    # blind is just a rubber stamp, and the record is what anyone reads back
    # months later when a salary deduction is queried.
    note = models.CharField("Reason", max_length=200, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="approved_leave")
    decided_at = models.DateTimeField(null=True, blank=True)
    # Why it was turned down. Kept separate from `note` so a rejection never
    # overwrites what the employee actually asked for.
    decision_note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    # Which partition this belongs to, so a partner's managers approve their own
    # people's leave and never see ours. Stamped from the subject's workspace —
    # the leave is a fact about them, not about whoever keyed it in.
    workspace = models.ForeignKey(
        "core.Workspace", on_delete=models.CASCADE, null=True, blank=True,
        related_name="leave_records")

    class Meta:
        ordering = ["-start_date", "-id"]
        indexes = [
            # The planner's hot path: "all leave overlapping this window".
            models.Index(fields=["start_date", "end_date"]),
            models.Index(fields=["user", "start_date"]),
            # The approvals queue: "what is still waiting on me".
            models.Index(fields=["status", "start_date"]),
        ]

    def __str__(self):
        who = self.user or "Everyone"
        return f"{who} · {self.get_kind_display()} · {self.start_date}–{self.end_date}"

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "The end date can't be before the start."})

    @property
    def is_company_wide(self):
        return self.user_id is None

    @property
    def days(self):
        return (self.end_date - self.start_date).days + 1

    @property
    def is_current(self):
        return self.start_date <= timezone.localdate() <= self.end_date

    @property
    def is_pending(self):
        return self.status == self.Status.REQUESTED

    @property
    def counts_against_allowance(self):
        return self.kind in self.ALLOWANCE_KINDS

    @property
    def is_unpaid(self):
        return self.kind in self.UNPAID_KINDS

    @property
    def is_retrospective(self):
        """Leave being recorded after the fact rather than asked for ahead.

        Shown to the approver, who is being asked to confirm something that
        already happened rather than to permit something that hasn't.
        """
        return self.end_date < timezone.localdate()

    @property
    def blocks_capacity(self):
        """Only approved leave removes capacity.

        A request that hasn't been approved yet is shown in the planner as a
        warning but does not change anybody's numbers — otherwise anyone could
        make themselves unavailable by asking.
        """
        return self.status == self.Status.APPROVED

    def overlaps(self, start, end):
        return self.start_date <= end and self.end_date >= start
