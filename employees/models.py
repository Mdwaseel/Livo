from django.conf import settings
from django.db import models
from django.urls import reverse

from core.models import TimeStampedModel


class Skill(models.Model):
    name = models.CharField(max_length=60, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class EmployeeProfile(TimeStampedModel):
    """HR profile hanging off User. Org fields (department, designation,
    reports_to, employee_code) and RBAC roles live on User itself; this model
    holds the person-facing profile plus payroll-gated sensitive fields.

    Company hardware/licenses are a separate future module — not here."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        PROBATION = "PROBATION", "Probation"
        NOTICE_PERIOD = "NOTICE_PERIOD", "Notice period"
        ON_LEAVE = "ON_LEAVE", "On leave"
        RESIGNED = "RESIGNED", "Resigned"
        TERMINATED = "TERMINATED", "Terminated"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="employee_profile")
    photo = models.ImageField(upload_to="employee_photos/", blank=True)
    # Lives here, on the HR profile, rather than in the calendar module that
    # displays it — a birthday is a fact about a person, and holding a second
    # copy of it elsewhere would be one more thing to keep in step. The calendar
    # reads this field and never emits the *year*: the whole agency can know
    # it's Anna's birthday on Thursday without also learning her age.
    date_of_birth = models.DateField(null=True, blank=True)
    date_of_joining = models.DateField(null=True, blank=True)
    date_of_exit = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices,
                              default=Status.ACTIVE)
    personal_email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    emergency_contact_name = models.CharField(max_length=150, blank=True)
    emergency_contact_phone = models.CharField(max_length=40, blank=True)
    skills = models.ManyToManyField(Skill, blank=True, related_name="employees")
    # Null means "whatever the agency default is", so raising the default lifts
    # everyone who has not been given a specific figure. Storing a copy of the
    # default on every profile would freeze today's number into history.
    annual_leave_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Paid leave days per year. Leave blank to use the agency "
                  "default from Agency settings.")

    # --- SENSITIVE: expose only behind has_perm(viewer, "payroll", "view") ---
    salary = models.DecimalField(max_digits=12, decimal_places=2,
                                 null=True, blank=True)
    bank_account_name = models.CharField(max_length=150, blank=True)
    bank_account_number = models.CharField(max_length=40, blank=True)
    bank_ifsc = models.CharField("Bank IFSC", max_length=20, blank=True)

    class Meta:
        ordering = ["user__first_name", "user__username"]

    def __str__(self):
        return self.user.get_full_name() or self.user.username

    def get_absolute_url(self):
        return reverse("employees:detail", args=[self.pk])

    @property
    def display_name(self):
        return self.user.get_full_name() or self.user.username


class SalaryRecord(TimeStampedModel):
    """One month's pay for one person: what was owed, what was withheld, what
    was actually paid and how.

    SENSITIVE — same gate as `EmployeeProfile.salary`, see
    `employees.views._can_view_sensitive`.

    Why a stored row rather than a figure computed on demand: every number here
    is a *snapshot of a decision*. `gross` is the salary as it stood that month,
    not as it stands today, so a raise in June does not silently rewrite May's
    payslip. The leave figures are frozen for the same reason — leave approved
    late, or cancelled afterwards, must not change a month that has already
    been paid. Recomputing would make history disagree with the bank statement.

    One row per employee per month, enforced by a unique constraint, because
    "how much did we pay Anna in May" has exactly one answer.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PAID = "PAID", "Paid"
        # Not deleted: a month somebody was not paid for is itself a fact, and
        # a missing row is indistinguishable from one nobody generated.
        SKIPPED = "SKIPPED", "Not payable"

    class Mode(models.TextChoices):
        BANK = "BANK", "Bank transfer"
        UPI = "UPI", "UPI"
        CASH = "CASH", "Cash"
        CHEQUE = "CHEQUE", "Cheque"
        OTHER = "OTHER", "Other"

    employee = models.ForeignKey(EmployeeProfile, on_delete=models.CASCADE,
                                 related_name="salary_records")
    # Always the 1st of the month it covers. A DateField rather than a
    # year/month pair so ordering, filtering and range queries are the ordinary
    # kind, and so `strftime` can label it without arithmetic.
    month = models.DateField(help_text="First day of the month this covers.")

    gross = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Frozen at generation. See the class docstring.
    leave_days_taken = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    leave_days_allowed = models.PositiveIntegerField(default=0)
    excess_leave_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    unpaid_leave_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    leave_deduction = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Anything the arithmetic cannot know: a bonus (positive), a recovery or an
    # advance (negative). Signed rather than two fields so the net is one sum.
    adjustment = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Bonus (positive) or other deduction (negative).")
    adjustment_note = models.CharField(max_length=200, blank=True)
    net_payable = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.PENDING)
    paid_on = models.DateField(null=True, blank=True)
    mode = models.CharField(max_length=10, choices=Mode.choices, blank=True)
    reference = models.CharField(
        max_length=120, blank=True,
        help_text="UTR, cheque number, transaction id — whatever ties this to "
                  "the bank record.")
    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    workspace = models.ForeignKey(
        "core.Workspace", on_delete=models.CASCADE, null=True, blank=True,
        related_name="salary_records")

    class Meta:
        ordering = ["-month", "employee__user__first_name"]
        constraints = [
            models.UniqueConstraint(fields=["employee", "month"],
                                    name="unique_salary_month"),
        ]
        indexes = [
            models.Index(fields=["month", "status"]),
            models.Index(fields=["workspace", "-month"]),
        ]

    def __str__(self):
        return f"{self.employee} · {self.month:%b %Y}"

    def recalculate(self):
        """net = gross − leave deduction + adjustment, never below zero.

        Clamped because a negative net payable is not a payment, it is a debt,
        and printing one on a payslip reads as a rendering fault. A recovery
        larger than the month's salary is a real situation, but it is spread
        across months by whoever runs accounts, not represented as owing us
        money on a payslip.
        """
        from decimal import Decimal
        net = (self.gross or Decimal("0")) - (self.leave_deduction or Decimal("0")) \
            + (self.adjustment or Decimal("0"))
        self.net_payable = max(net, Decimal("0"))
        return self.net_payable

    @property
    def is_paid(self):
        return self.status == self.Status.PAID

    @property
    def total_deduction(self):
        from decimal import Decimal
        negative_adjustment = min(self.adjustment or Decimal("0"), Decimal("0"))
        return (self.leave_deduction or Decimal("0")) - negative_adjustment


class EmployeeDocument(TimeStampedModel):
    """Identity/HR documents. ALL doc types are sensitive: viewable only with
    payroll.view or by the employee themself."""

    class DocType(models.TextChoices):
        OFFER_LETTER = "OFFER_LETTER", "Offer letter"
        NDA = "NDA", "NDA"
        PAN = "PAN", "PAN card"
        AADHAAR = "AADHAAR", "Aadhaar"
        RESUME = "RESUME", "Resume"
        EXPERIENCE = "EXPERIENCE", "Experience letter"
        OTHER = "OTHER", "Other"

    employee = models.ForeignKey(EmployeeProfile, on_delete=models.CASCADE,
                                 related_name="documents")
    doc_type = models.CharField(max_length=20, choices=DocType.choices,
                                default=DocType.OTHER)
    # Either a stored file or, for anything over the upload ceiling, a link to
    # where it lives instead. See core.uploads for the ceiling and why.
    file = models.FileField(upload_to="employee_docs/", blank=True)
    external_url = models.URLField(
        max_length=500, blank=True,
        help_text="Google Drive (or similar) link, used for files over the size limit")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                    on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_doc_type_display()} · {self.employee}"

    @property
    def is_link(self):
        return not self.file and bool(self.external_url)

    @property
    def filename(self):
        if self.is_link:
            return self.notes or self.external_url
        return self.file.name.rsplit("/", 1)[-1] if self.file else ""
