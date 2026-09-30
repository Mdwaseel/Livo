"""Salary records: the monthly payroll screen and everyone's own payslip history.

Two audiences, two gates:

* **My salary** — anyone signed in, their own rows only. What you were paid,
  when, by what method, and what was withheld. Your own pay is yours to see;
  that is already how `EmployeeProfile.salary` behaves on the profile page.
* **Payroll** — `payroll.view` to read, `payroll.edit` to generate a month or
  mark a row paid. Workspace-scoped on top, so a partner runs their own payroll
  and never sees ours (see `employees.views._can_view_sensitive`).

The arithmetic lives in `employees.payroll` and `employees.leave`; these views
parse a month, ask for numbers, and render.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import has_perm
from core.models import log_activity, notify
from core.tenancy import active_workspace_ids, scope

from . import leave as leave_math
from . import payroll
from .models import EmployeeProfile, SalaryRecord


# ---------------------------------------------------------------------------
# permissions
# ---------------------------------------------------------------------------

def can_view_payroll(user):
    """Read the payroll screen.

    The workspace clause is the partner rule, not an RBAC one: a partner super
    admin passes every `has_perm` check there is, so without it running a
    partner workspace would come with our salary bill attached.
    """
    # The partition is applied to the ROWS, by `visible_salary` below, rather
    # than to the door — a partner genuinely does run payroll, just only for
    # their own people. This is the one payroll surface where that is true;
    # an individual's salary on their profile page is gated per-person by
    # `employees.views._can_view_sensitive`.
    return has_perm(user, "payroll", "view")


def can_run_payroll(user):
    return can_view_payroll(user) and has_perm(user, "payroll", "edit")


def _payroll_required(request):
    if not can_view_payroll(request.user):
        messages.error(request, "You don't have access to payroll.")
        return redirect("core:dashboard")
    return None


def _parse_month(raw):
    """'YYYY-MM' -> the 1st of that month. Junk falls back to this month."""
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m").date().replace(day=1)
    except ValueError:
        return timezone.localdate().replace(day=1)


def _parse_date(raw):
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _decimal_or_zero(raw):
    raw = (raw or "").strip()
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _month_url(month):
    return f"{reverse('employees:payroll')}?month={month:%Y-%m}"


def visible_salary(user):
    """Salary rows in this viewer's workspace scope."""
    return scope(
        SalaryRecord.objects.select_related(
            "employee", "employee__user", "employee__user__department"),
        user)


# ---------------------------------------------------------------------------
# my salary
# ---------------------------------------------------------------------------

@login_required
def my_salary(request):
    profile, _ = EmployeeProfile.objects.get_or_create(user=request.user)
    records = (SalaryRecord.objects.filter(employee=profile)
               .order_by("-month"))
    return render(request, "employees/salary_mine.html", {
        "profile": profile,
        "records": records[:36],
        "balance": leave_math.balance(profile),
        "total_paid": sum((r.net_payable for r in records if r.is_paid),
                          start=Decimal("0")),
    })


# ---------------------------------------------------------------------------
# payroll
# ---------------------------------------------------------------------------

@login_required
def payroll_month(request):
    denial = _payroll_required(request)
    if denial:
        return denial

    month = _parse_month(request.GET.get("month"))
    records = list(visible_salary(request.user).filter(month=month)
                   .order_by("employee__user__first_name"))
    generated = {record.employee_id for record in records}

    # People with no row yet, so the screen shows who generating would add
    # rather than silently leaving them out.
    missing = [
        profile for profile in payroll.payable_profiles(
            month, workspace_ids=active_workspace_ids(request.user))
        if profile.pk not in generated
    ]

    return render(request, "employees/payroll.html", {
        "month": month,
        "month_input": month.strftime("%Y-%m"),
        "records": records,
        "missing": missing,
        "totals": payroll.month_totals(records),
        # Counted here rather than filtered in the template: the summary strip
        # says "3 of 5 settled", and a template cannot count a property.
        "paid_count": sum(1 for record in records if record.is_paid),
        "can_run": can_run_payroll(request.user),
        "modes": SalaryRecord.Mode.choices,
        "today": timezone.localdate(),
    })


@login_required
@require_POST
def payroll_generate(request):
    denial = _payroll_required(request)
    if denial:
        return denial
    if not can_run_payroll(request.user):
        messages.error(request, "You don't have permission to run payroll.")
        return redirect("employees:payroll")

    month = _parse_month(request.POST.get("month"))
    summary = payroll.generate_month(
        month, actor=request.user,
        workspace_ids=active_workspace_ids(request.user))

    log_activity(request.user, "generated payroll", month.strftime("%b %Y"),
                 f"{summary['created']} new, {summary['updated']} updated")
    parts = []
    if summary["created"]:
        parts.append(f"{summary['created']} created")
    if summary["updated"]:
        parts.append(f"{summary['updated']} recalculated")
    if summary["kept"]:
        parts.append(f"{summary['kept']} already paid, left alone")
    messages.success(
        request,
        f"Payroll for {month:%B %Y}: " + (", ".join(parts) if parts
                                          else "nobody to pay."))
    return redirect(_month_url(month))


@login_required
@require_POST
def salary_pay(request, pk):
    """Mark one row paid, with how and when."""
    denial = _payroll_required(request)
    if denial:
        return denial
    if not can_run_payroll(request.user):
        messages.error(request, "You don't have permission to record payments.")
        return redirect("employees:payroll")

    record = get_object_or_404(visible_salary(request.user), pk=pk)
    paid_on = _parse_date(request.POST.get("paid_on")) or timezone.localdate()
    mode = request.POST.get("mode", "")
    if mode not in SalaryRecord.Mode.values:
        messages.error(request, "Pick how the salary was paid.")
        return redirect(_month_url(record.month))

    payroll.mark_paid(record, paid_on=paid_on, mode=mode,
                      reference=request.POST.get("reference", ""),
                      note=request.POST.get("note", ""), actor=request.user)
    log_activity(request.user, "recorded salary payment",
                 f"{record.employee.display_name} · {record.month:%b %Y}",
                 f"₹{record.net_payable} by {record.get_mode_display()}")
    notify([record.employee.user],
           f"Salary for {record.month:%B %Y} paid — ₹{record.net_payable}",
           url=reverse("employees:my_salary"), exclude=request.user)
    messages.success(request,
                     f"Recorded ₹{record.net_payable} paid to "
                     f"{record.employee.display_name}.")
    return redirect(_month_url(record.month))


@login_required
@require_POST
def salary_adjust(request, pk):
    """A bonus, or a deduction the leave arithmetic cannot know about."""
    denial = _payroll_required(request)
    if denial:
        return denial
    if not can_run_payroll(request.user):
        messages.error(request, "You don't have permission to edit payroll.")
        return redirect("employees:payroll")

    record = get_object_or_404(visible_salary(request.user), pk=pk)
    if record.is_paid:
        messages.error(request,
                       "That month is already paid — adjust the next one instead.")
        return redirect(_month_url(record.month))

    record.adjustment = _decimal_or_zero(request.POST.get("adjustment"))
    record.adjustment_note = (request.POST.get("adjustment_note") or "")[:200]
    record.recalculate()
    record.save(update_fields=["adjustment", "adjustment_note", "net_payable",
                               "updated_at"])
    log_activity(request.user, "adjusted salary",
                 f"{record.employee.display_name} · {record.month:%b %Y}",
                 f"₹{record.adjustment} — {record.adjustment_note}")
    messages.success(request, "Adjustment saved.")
    return redirect(_month_url(record.month))
