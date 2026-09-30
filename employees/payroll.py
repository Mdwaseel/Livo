"""Turning a month's leave into a month's pay.

One job: build (or refresh) a `SalaryRecord` per employee per month, with the
leave deduction worked out by `employees.leave` so the figure on a payslip and
the balance on somebody's own leave page are the same arithmetic.

**Generating is safe to repeat.** Running it twice for May does not create two
rows or double a deduction — the unique constraint on (employee, month) makes
that structurally impossible, and a row that is already **paid** is never
touched again. That last rule is the important one: once money has left the
bank, the record of it is history, and history does not get recalculated
because somebody approved a leave request late.

**Who is included.** Anyone still on the books that month. People who resigned
before it started, or who joined after it ended, are skipped — a payslip for a
month somebody did not work for us is not a zero, it is a mistake.
"""
from decimal import Decimal

from django.db import transaction

from .leave import deduction_for_month, month_bounds
from .models import EmployeeProfile, SalaryRecord

ZERO = Decimal("0")

# Statuses that mean somebody has left. Their last month still generates — they
# worked part of it — but months after their exit date do not.
GONE = (EmployeeProfile.Status.RESIGNED, EmployeeProfile.Status.TERMINATED)


def employed_during(profile, first, last):
    """Was this person on the books at any point in the month?"""
    if profile.date_of_joining and profile.date_of_joining > last:
        return False
    if profile.date_of_exit and profile.date_of_exit < first:
        return False
    # No exit date but marked gone: trust the status only when there is no date
    # to be more precise with.
    if profile.status in GONE and not profile.date_of_exit:
        return False
    return True


def payable_profiles(month_start, *, workspace_ids=None):
    """Everyone a payslip should exist for in this month."""
    first, last = month_bounds(month_start)
    profiles = (EmployeeProfile.objects
                .select_related("user", "user__workspace", "user__department")
                .order_by("user__first_name", "user__username"))
    if workspace_ids is not None:
        from django.db.models import Q
        profiles = profiles.filter(
            Q(user__workspace_id__in=workspace_ids)
            | Q(user__workspace__isnull=True))
    return [p for p in profiles if employed_during(p, first, last)]


def build_record(profile, month_start, *, actor=None):
    """Create or refresh one person's row for one month. Returns (record, action).

    `action` is "created", "updated" or "kept" — the caller reports what
    actually happened rather than claiming to have generated rows it left alone.
    """
    first, _ = month_bounds(month_start)
    figures = deduction_for_month(profile, first)

    record = SalaryRecord.objects.filter(employee=profile, month=first).first()
    if record is not None and record.is_paid:
        # Settled. See the module docstring.
        return record, "kept"

    created = record is None
    if created:
        record = SalaryRecord(employee=profile, month=first)

    record.gross = figures["gross"]
    record.leave_days_taken = figures["taken_this_month"]
    record.leave_days_allowed = figures["allowed"]
    record.excess_leave_days = figures["excess_days"]
    record.unpaid_leave_days = figures["unpaid_days"]
    record.leave_deduction = figures["deduction"]
    # `adjustment` is deliberately not touched: a bonus somebody keyed in by
    # hand must survive a regenerate, or nobody will trust the button.
    record.workspace_id = profile.user.workspace_id
    if actor is not None and record.recorded_by_id is None:
        record.recorded_by = actor
    record.recalculate()
    record.save()
    return record, "created" if created else "updated"


@transaction.atomic
def generate_month(month_start, *, actor=None, workspace_ids=None):
    """Build the whole month. Returns a {action: count} summary."""
    summary = {"created": 0, "updated": 0, "kept": 0}
    for profile in payable_profiles(month_start, workspace_ids=workspace_ids):
        _, action = build_record(profile, month_start, actor=actor)
        summary[action] += 1
    return summary


def mark_paid(record, *, paid_on, mode, reference="", note="", actor=None):
    """Record that the money actually went out."""
    record.status = SalaryRecord.Status.PAID
    record.paid_on = paid_on
    record.mode = mode
    record.reference = reference[:120]
    if note:
        record.note = note[:200]
    if actor is not None:
        record.recorded_by = actor
    record.recalculate()
    record.save()
    return record


def month_totals(records):
    """Footer figures for the payroll table."""
    totals = {"gross": ZERO, "deduction": ZERO, "net": ZERO,
              "paid": ZERO, "outstanding": ZERO}
    for record in records:
        totals["gross"] += record.gross or ZERO
        totals["deduction"] += record.leave_deduction or ZERO
        totals["net"] += record.net_payable or ZERO
        if record.is_paid:
            totals["paid"] += record.net_payable or ZERO
        elif record.status == SalaryRecord.Status.PENDING:
            totals["outstanding"] += record.net_payable or ZERO
    return totals
