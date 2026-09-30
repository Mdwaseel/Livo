"""Leave self-service and approvals.

Three screens:

* **My leave** — anyone signed in. Their balance, their history, and the two
  ways to add to it: asking for time off ahead, or recording time already
  taken. No RBAC action gates this; asking for leave is not a privilege.
* **Approvals** — whoever can approve. Everything waiting, oldest first.
* **A person's leave** — an approver looking at one employee's record.

The planner at /planning/leave/ keeps its own screen for the *capacity* view of
the same table — who is away next week. This is the HR view of it: whose leave,
how much is left, and what the excess will cost. Both read `LeaveRecord`; there
is no second table and no second definition of a leave day.

**Who approves what.** The employee's own manager (`User.reports_to`) if they
have one, and anyone holding `leaves.approve` regardless — the same routing
work-log approvals already use, so nobody has to learn a second rule. A super
admin holds every action and therefore approves anything, which is what was
asked for.

**Nobody approves their own.** Checked server-side rather than only hidden in
the template, because a self-approval is precisely the request somebody would
be motivated to hand-craft.
"""
from datetime import datetime

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import has_perm, users_with_perm
from core.models import log_activity, notify
from core.tenancy import scope, workspace_for_new
from resource_planner.models import LeaveRecord

from . import leave as leave_math
from .models import EmployeeProfile

User = get_user_model()

# What somebody may file for themselves. Public holidays are a company-wide
# fact entered by whoever runs the calendar, not a thing an individual takes.
SELF_SERVICE_KINDS = [
    (value, label) for value, label in LeaveRecord.Kind.choices
    if value != LeaveRecord.Kind.HOLIDAY
]


# ---------------------------------------------------------------------------
# permissions
# ---------------------------------------------------------------------------

def can_approve(user):
    return has_perm(user, "leaves", "approve")


def can_manage_others(user):
    """File or edit leave on somebody else's behalf — HR keying in a form."""
    return has_perm(user, "leaves", "create") or has_perm(user, "leaves", "edit")


def approvable_by(user, record):
    """May THIS person decide THIS request?"""
    if record.user_id == getattr(user, "pk", None):
        return False                      # never your own
    if record.user_id is None:
        return can_manage_others(user)    # company-wide holiday
    if can_approve(user):
        return True
    return record.user.reports_to_id == user.pk


def visible_leave(user):
    """Leave records this person may read.

    Workspace first — a partner's manager approves their own people and never
    sees ours. Then: your own always, everyone's if you can approve, and your
    direct reports' if you are their manager without the blanket action.
    """
    records = scope(
        LeaveRecord.objects.select_related("user", "approved_by", "created_by"),
        user)
    if can_approve(user) or can_manage_others(user):
        return records
    return records.filter(Q(user=user) | Q(user__reports_to=user))


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def _parse_date(raw):
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _profile_for(user):
    profile, _ = EmployeeProfile.objects.get_or_create(user=user)
    return profile


# ---------------------------------------------------------------------------
# my leave
# ---------------------------------------------------------------------------

@login_required
def my_leave(request):
    """Balance, history, and the forms to add to it."""
    profile = _profile_for(request.user)
    year = timezone.localdate().year
    records = (LeaveRecord.objects.filter(user=request.user)
               .select_related("approved_by")
               .order_by("-start_date"))
    return render(request, "employees/leave_mine.html", {
        "profile": profile,
        "balance": leave_math.balance(profile, year),
        "records": records[:60],
        "kinds": SELF_SERVICE_KINDS,
        "today": timezone.localdate(),
        "daily_rate": leave_math.daily_rate(profile.salary),
        # Their own pay is theirs to see, so the cost of going over is shown
        # to them — being told a deduction is coming only on payday is how a
        # policy like this turns into a grievance.
        "show_cost": profile.salary is not None,
    })


@login_required
@require_POST
def leave_request(request):
    """File leave for yourself — future ("request") or past ("record").

    Both land as REQUESTED. A retrospective entry is still somebody's manager
    confirming it happened; auto-approving it would make the register a diary
    anyone could write into, and it feeds a salary deduction.
    """
    start = _parse_date(request.POST.get("start_date"))
    end = _parse_date(request.POST.get("end_date")) or start
    kind = request.POST.get("kind", LeaveRecord.Kind.VACATION)
    reason = (request.POST.get("note") or "").strip()

    if start is None or end is None:
        messages.error(request, "Pick the dates you'll be away.")
        return redirect("employees:my_leave")
    if end < start:
        messages.error(request, "The end date can't be before the start.")
        return redirect("employees:my_leave")
    if not reason:
        messages.error(request, "Add a short reason — your approver needs it.")
        return redirect("employees:my_leave")
    if kind not in dict(SELF_SERVICE_KINDS):
        kind = LeaveRecord.Kind.OTHER

    overlapping = LeaveRecord.objects.filter(
        user=request.user, start_date__lte=end, end_date__gte=start,
    ).exclude(status__in=(LeaveRecord.Status.REJECTED,
                          LeaveRecord.Status.CANCELLED)).first()
    if overlapping is not None:
        messages.error(
            request,
            f"You already have leave from {overlapping.start_date:%d %b} to "
            f"{overlapping.end_date:%d %b}. Cancel it first if this replaces it.")
        return redirect("employees:my_leave")

    record = LeaveRecord(
        user=request.user, kind=kind, start_date=start, end_date=end,
        is_half_day=bool(request.POST.get("is_half_day")),
        note=reason[:200], status=LeaveRecord.Status.REQUESTED,
        created_by=request.user,
        workspace=workspace_for_new(request.user),
    )
    # The top of the tree has nobody to ask. Somebody who can approve leave and
    # reports to no one — the agency owner, typically — would otherwise file
    # requests that sit pending for ever: never counting against an allowance,
    # never reaching a payslip, and never appearing in anyone's queue, because
    # nobody may approve their own. So their own entries are self-evidently
    # decided at the point of filing. Everyone with a manager still goes through
    # them, super admin or not.
    decides_for_themselves = (can_approve(request.user)
                              and request.user.reports_to_id is None)
    if decides_for_themselves:
        record.status = LeaveRecord.Status.APPROVED
        record.approved_by = request.user
        record.decided_at = timezone.now()

    try:
        record.full_clean(exclude=["user", "approved_by", "created_by",
                                   "workspace"])
    except ValidationError as error:
        messages.error(request, "; ".join(
            message for group in error.message_dict.values() for message in group))
        return redirect("employees:my_leave")
    record.save()

    days = leave_math.countable_days(record)
    if decides_for_themselves:
        log_activity(request.user, "recorded own leave",
                     record.get_kind_display(),
                     f"{record.start_date}–{record.end_date}")
        messages.success(
            request,
            f"Recorded {days} day{'' if days == 1 else 's'} of "
            f"{record.get_kind_display().lower()}. You have no approver above "
            "you, so it's logged straight away.")
        return redirect("employees:my_leave")

    _notify_approvers(request, record)
    log_activity(request.user, "requested leave", record.get_kind_display(),
                 f"{record.start_date}–{record.end_date}")
    messages.success(
        request,
        f"Sent for approval: {days} day{'' if days == 1 else 's'} of "
        f"{record.get_kind_display().lower()}.")
    return redirect("employees:my_leave")


@login_required
@require_POST
def leave_cancel(request, pk):
    """Withdraw your own request, or approved leave you no longer need.

    Cancelled rather than deleted: a request somebody approved and then saw
    vanish is a support call, and the record is what a salary deduction is
    later justified against.
    """
    record = get_object_or_404(LeaveRecord, pk=pk, user=request.user)
    if record.status in (LeaveRecord.Status.REJECTED,
                         LeaveRecord.Status.CANCELLED):
        messages.error(request, "That leave is already closed.")
        return redirect("employees:my_leave")
    record.status = LeaveRecord.Status.CANCELLED
    record.decided_at = timezone.now()
    record.save(update_fields=["status", "decided_at", "updated_at"])
    log_activity(request.user, "cancelled leave", record.get_kind_display(),
                 f"{record.start_date}–{record.end_date}")
    messages.success(request, "Leave cancelled.")
    return redirect("employees:my_leave")


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------

@login_required
def approvals(request):
    """Everything waiting on this approver, oldest request first."""
    if not (can_approve(request.user) or can_manage_others(request.user)
            or User.objects.filter(reports_to=request.user).exists()):
        messages.error(request, "You don't approve leave for anyone.")
        return redirect("employees:my_leave")

    pending = [
        record for record in visible_leave(request.user)
        .filter(status=LeaveRecord.Status.REQUESTED).order_by("start_date")
        if approvable_by(request.user, record)
    ]
    # The balance is the context an approver actually needs: "three days left"
    # and "already four days over" are different decisions on the same request.
    profiles = {
        profile.user_id: profile
        for profile in EmployeeProfile.objects.filter(
            user_id__in={r.user_id for r in pending if r.user_id})
    }
    rows = []
    for record in pending:
        profile = profiles.get(record.user_id)
        rows.append({
            "record": record,
            "days": leave_math.countable_days(record),
            "balance": leave_math.balance(profile) if profile else None,
        })

    decided = (visible_leave(request.user)
               .exclude(status=LeaveRecord.Status.REQUESTED)
               .order_by("-decided_at", "-start_date")[:25])
    return render(request, "employees/leave_approvals.html", {
        "rows": rows,
        "decided": decided,
        "today": timezone.localdate(),
    })


@login_required
@require_POST
def leave_decide(request, pk):
    record = get_object_or_404(
        visible_leave(request.user).filter(status=LeaveRecord.Status.REQUESTED),
        pk=pk)
    if not approvable_by(request.user, record):
        messages.error(
            request,
            "You can't decide that request."
            if record.user_id != request.user.pk
            else "You can't approve your own leave.")
        return redirect("employees:leave_approvals")

    action = request.POST.get("action")
    if action not in ("approve", "reject"):
        messages.error(request, "Unknown action.")
        return redirect("employees:leave_approvals")

    record.status = (LeaveRecord.Status.APPROVED if action == "approve"
                     else LeaveRecord.Status.REJECTED)
    record.approved_by = request.user
    record.decided_at = timezone.now()
    record.decision_note = (request.POST.get("decision_note") or "").strip()[:200]
    record.save(update_fields=["status", "approved_by", "decided_at",
                               "decision_note", "updated_at"])

    verb = "approved" if action == "approve" else "rejected"
    if record.user:
        detail = f" — {record.decision_note}" if record.decision_note else ""
        notify([record.user],
               f"Leave {verb}: {record.start_date:%d %b}–"
               f"{record.end_date:%d %b}{detail}"[:220],
               url=reverse("employees:my_leave"), exclude=request.user)
    log_activity(request.user, f"{verb} leave", str(record.user or "Everyone"),
                 f"{record.start_date}–{record.end_date}")
    messages.success(request, f"Leave {verb}.")
    return redirect("employees:leave_approvals")


def _notify_approvers(request, record):
    """Manager first, then anyone holding `leaves.approve`.

    Same routing as work-log approvals (`projects.views._notify_approvers`), and
    workspace-scoped for the same reason that one is: a partner's leave request
    is not our managers' business.
    """
    manager = record.user.reports_to if record.user else None
    if manager is not None and manager.is_active:
        recipients = [manager]
    else:
        recipients = list(users_with_perm(
            "leaves", "approve", workspace=record.workspace_id))
    if not recipients:
        return
    who = request.user.get_full_name() or request.user.username
    window = f"{record.start_date:%d %b}"
    if record.end_date != record.start_date:
        window += f"–{record.end_date:%d %b}"
    notify(recipients,
           f"{who} requested {record.get_kind_display().lower()} · {window}"[:220],
           url=reverse("employees:leave_approvals"), exclude=request.user)
