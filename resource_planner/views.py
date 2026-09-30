"""Resource planning screens.

Five read views, two editors and one JSON endpoint. Every view parses a
`Window`, asks `services` for rows, and renders — the arithmetic is all
testable without a request, and the ORM is all in `selectors`.
"""
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import log_activity, notify
from core.tenancy import scope, workspace_for_new
from projects.access import ensure_member
from projects.emails import send_task_assigned
from projects.models import Project, Task

from . import capacity as cap
from . import selectors, services
from .models import CapacityProfile, LeaveRecord
from .permissions import (can_approve_leave, can_assign_tasks, can_edit_capacity,
                          can_manage_leave, capacity_edit_required, planner_required)

User = get_user_model()


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------

def _parse_date(raw, fallback):
    try:
        return date.fromisoformat((raw or "").strip())
    except (ValueError, AttributeError):
        return fallback


def _parse_int(raw):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _window_from_request(request, *, mode="week"):
    """Anchor date + scope filters, shared by every planner screen."""
    today = timezone.localdate()
    anchor = _parse_date(request.GET.get("date"), today)
    scope = {
        "department_id": _parse_int(request.GET.get("department")),
        "user_id": _parse_int(request.GET.get("employee")),
        "project_id": _parse_int(request.GET.get("project")),
    }
    if mode == "month":
        return services.Window.for_month(anchor, **scope)
    return services.Window.for_week(anchor, **scope)


def _base_context(request, window):
    return {
        "window": window,
        "today": timezone.localdate(),
        "departments": selectors.departments(),
        "people": selectors.plannable_users(),
        "projects": scope(Project.objects.filter(is_archived=False))
                    .select_related("client").only("id", "name", "client__name")
                    .order_by("client__name", "name"),
        "can_edit_capacity": can_edit_capacity(request.user),
        "can_assign": can_assign_tasks(request.user),
        "can_manage_leave": can_manage_leave(request.user),
        "status_labels": cap.STATUS_LABELS,
    }


# ---------------------------------------------------------------------------
# 1. overview — who is free, who is drowning
# ---------------------------------------------------------------------------

@login_required
@planner_required
def overview(request):
    window = _window_from_request(request)
    rows = services.workload(window)
    context = _base_context(request, window)
    context.update({
        "rows": rows,
        "summary": services.summarise(rows),
        "board": services.availability_board(rows),
        "overbooking": services.overbooking_report(rows),
        "leaders": services.utilization_leaders(rows),
        "forecast": services.capacity_forecast(
            weeks=8, department_id=window.department_id),
        "active": "overview",
    })
    return render(request, "resource_planner/overview.html", context)


# ---------------------------------------------------------------------------
# 2. employee workload
# ---------------------------------------------------------------------------

@login_required
@planner_required
def employees(request):
    window = _window_from_request(request)
    rows = services.workload(window)
    context = _base_context(request, window)
    context.update({
        "rows": rows,
        "summary": services.summarise(rows),
        "active": "employees",
    })
    return render(request, "resource_planner/employees.html", context)


# ---------------------------------------------------------------------------
# 3. department workload
# ---------------------------------------------------------------------------

@login_required
@planner_required
def departments(request):
    window = _window_from_request(request)
    rows = services.department_workload(window)
    context = _base_context(request, window)
    context.update({
        "rows": rows,
        "agency": services.summarise(
            [member for row in rows for member in row["members"]]),
        "active": "departments",
    })
    return render(request, "resource_planner/departments.html", context)


# ---------------------------------------------------------------------------
# 4. project allocation
# ---------------------------------------------------------------------------

@login_required
@planner_required
def projects(request):
    window = _window_from_request(request)
    rows = selectors.project_allocation_rows(
        window.start, window.end, project_id=window.project_id)
    # Projects with nobody on them are the interesting ones, so they stay in the
    # list rather than being filtered out — sorted last, but present.
    rows.sort(key=lambda row: (not row["members"], row["project"].name))
    context = _base_context(request, window)
    context.update({
        "rows": rows,
        "unassigned": selectors.unassigned_open_tasks(
            project_id=window.project_id),
        "active": "projects",
    })
    return render(request, "resource_planner/projects.html", context)


# ---------------------------------------------------------------------------
# 5 + 6. weekly and monthly planners (the heatmap + drag-and-drop grid)
# ---------------------------------------------------------------------------

@login_required
@planner_required
def weekly(request):
    window = _window_from_request(request, mode="week")
    grid = services.planner_grid(window)
    context = _base_context(request, window)
    context.update({
        "grid": grid,
        "unassigned": selectors.unassigned_open_tasks(
            project_id=window.project_id, limit=25),
        "summary": services.summarise(services.workload(window)),
        "active": "weekly",
        "planner_mode": "week",
    })
    return render(request, "resource_planner/planner.html", context)


@login_required
@planner_required
def monthly(request):
    window = _window_from_request(request, mode="month")
    grid = services.planner_grid(window)
    context = _base_context(request, window)
    context.update({
        "grid": grid,
        "unassigned": selectors.unassigned_open_tasks(
            project_id=window.project_id, limit=25),
        "summary": services.summarise(services.workload(window)),
        "active": "monthly",
        "planner_mode": "month",
    })
    return render(request, "resource_planner/planner.html", context)


# ---------------------------------------------------------------------------
# drag-and-drop reassignment
# ---------------------------------------------------------------------------

@login_required
@require_POST
def reassign(request):
    """Move a task to another person and/or another day.

    Returns JSON so the grid can update in place. Gated on `tasks.assign`, the
    same permission the task page uses — the planner must not become a way
    around it.

    An overbooking move is allowed and *warned about*, not blocked: a manager
    reassigning into an overload usually knows something the estimate does not,
    and a planner that refuses the drop just gets worked around outside the
    tool.
    """
    if not can_assign_tasks(request.user):
        return JsonResponse(
            {"ok": False, "error": "You don't have permission to reassign tasks."},
            status=403)

    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Malformed request."}, status=400)

    task = scope(Task.objects, path="project__workspace").filter(
        pk=_parse_int(payload.get("task"))).select_related(
        "project", "assignee").first()
    if task is None:
        return JsonResponse({"ok": False, "error": "That task no longer exists."},
                            status=404)

    previous_assignee = task.assignee
    changes = []

    # A null assignee is meaningful — it drops the task back to the backlog.
    if "assignee" in payload:
        assignee_id = _parse_int(payload.get("assignee"))
        assignee = None
        if assignee_id:
            assignee = User.objects.filter(pk=assignee_id, is_active=True).first()
            if assignee is None:
                return JsonResponse(
                    {"ok": False, "error": "That person isn't available."}, status=400)
        if assignee != task.assignee:
            task.assignee = assignee
            changes.append("assignee")

    if payload.get("due_date"):
        new_due = _parse_date(payload["due_date"], None)
        if new_due is None:
            return JsonResponse({"ok": False, "error": "Invalid date."}, status=400)
        if new_due != task.due_date:
            task.due_date = new_due
            changes.append("due_date")

    if not changes:
        return JsonResponse({"ok": True, "unchanged": True})

    task.save(update_fields=changes + ["updated_at"])
    log_activity(request.user, "reassigned task",
                 f"{task.title} · {task.project.name}",
                 f"{previous_assignee or 'Unassigned'} → {task.assignee or 'Unassigned'}")
    # The person picking up the work should hear about it from the app, not
    # from noticing it on a board. notify() takes an iterable and skips the
    # actor itself via `exclude`.
    if task.assignee and task.assignee != previous_assignee:
        notify([task.assignee], f"{task.title} was assigned to you",
               task.get_absolute_url(), exclude=request.user)
        # Dragging work onto someone puts them on the project, same as every
        # other assignment route — see projects.access.ensure_member.
        ensure_member(task.project, task.assignee)
        send_task_assigned(task, actor=request.user, request=request)

    warning = ""
    if task.assignee:
        window = services.Window.for_week(task.due_date or timezone.localdate())
        window.user_id = task.assignee_id
        rows = services.workload(window)
        if rows:
            row = rows[0]
            if row["remaining_capacity"] < 0:
                warning = (
                    f"{row['user'].get_full_name() or row['user'].get_username()} "
                    f"is now {abs(row['remaining_capacity'])} h over capacity "
                    f"that week ({row['utilization_percent']:.0f}%).")

    return JsonResponse({
        "ok": True,
        "task": task.pk,
        "assignee": task.assignee_id,
        "assignee_name": (task.assignee.get_full_name() or
                          task.assignee.get_username()) if task.assignee else "",
        "due_date": task.due_date.isoformat() if task.due_date else "",
        "warning": warning,
    })


# ---------------------------------------------------------------------------
# capacity configuration
# ---------------------------------------------------------------------------

@login_required
@capacity_edit_required
def capacity_edit(request, user_pk):
    person = get_object_or_404(User, pk=user_pk)
    profile = (CapacityProfile.objects.filter(user=person).first()
               or CapacityProfile.default_for(person))

    if request.method == "POST":
        try:
            profile.daily_hours = _decimal(request.POST.get("daily_hours"),
                                           minimum=Decimal("0"),
                                           maximum=Decimal("24"))
            picked = "".join(sorted(set(request.POST.getlist("working_days"))))
            if not picked:
                # Checking this here, not in the model validator: an empty
                # CharField trips `blank=False` first and the user gets "This
                # field cannot be blank", which says nothing about a day picker.
                raise ValueError("Pick at least one working day.")
            profile.working_days = picked
            profile.weekly_hours_override = _optional_decimal(
                request.POST.get("weekly_hours_override"))
            profile.monthly_hours_override = _optional_decimal(
                request.POST.get("monthly_hours_override"))
            profile.notes = request.POST.get("notes", "")[:200]
            profile.updated_by = request.user
            profile.full_clean(exclude=["user"])
        except ValidationError as error:
            messages.error(request, "; ".join(
                message for messages_ in error.message_dict.values()
                for message in messages_))
            return redirect(request.path)
        except ValueError as error:
            messages.error(request, str(error))
            return redirect(request.path)

        profile.save()
        log_activity(request.user, "updated capacity",
                     person.get_full_name() or person.get_username(),
                     f"{profile.daily_hours} h/day · days {profile.working_days}")
        messages.success(request, "Capacity updated.")
        return redirect("resource_planner:employees")

    return render(request, "resource_planner/capacity_form.html", {
        "person": person,
        "profile": profile,
        "is_new": profile.pk is None,
        "weekday_choices": [(1, "Mon"), (2, "Tue"), (3, "Wed"), (4, "Thu"),
                            (5, "Fri"), (6, "Sat"), (7, "Sun")],
        "selected_days": profile.working_day_numbers,
    })


def _decimal(raw, *, minimum=None, maximum=None):
    try:
        value = Decimal((raw or "").strip())
    except (InvalidOperation, AttributeError):
        raise ValueError("Enter hours as a number, for example 7.5.")
    if minimum is not None and value < minimum:
        raise ValueError("Hours can't be negative.")
    if maximum is not None and value > maximum:
        raise ValueError("There are only 24 hours in a day.")
    return value


def _optional_decimal(raw):
    if not (raw or "").strip():
        return None
    return _decimal(raw, minimum=Decimal("0"))


# ---------------------------------------------------------------------------
# leave
# ---------------------------------------------------------------------------

@login_required
@planner_required
def leave_list(request):
    today = timezone.localdate()
    horizon = today + timedelta(days=180)
    # Scoped through the subject rather than the row: company-wide holidays
    # carry no user and no workspace, and they apply to everybody, so they are
    # let through explicitly.
    records = (scope(LeaveRecord.objects, path="user__workspace")
               .filter(end_date__gte=today - timedelta(days=30),
                       start_date__lte=horizon)
               .select_related("user", "approved_by")
               .order_by("start_date"))
    return render(request, "resource_planner/leave.html", {
        "records": records,
        "people": selectors.plannable_users(),
        "kinds": LeaveRecord.Kind.choices,
        "statuses": LeaveRecord.Status.choices,
        "can_manage_leave": can_manage_leave(request.user),
        "can_approve_leave": can_approve_leave(request.user),
        "today": today,
        "active": "leave",
    })


@login_required
@require_POST
def leave_create(request):
    if not can_manage_leave(request.user):
        messages.error(request, "You don't have permission to record leave.")
        return redirect("resource_planner:leave")

    record = LeaveRecord(
        user_id=_parse_int(request.POST.get("user")),
        kind=request.POST.get("kind", LeaveRecord.Kind.VACATION),
        start_date=_parse_date(request.POST.get("start_date"), None),
        end_date=_parse_date(request.POST.get("end_date"), None),
        is_half_day=bool(request.POST.get("is_half_day")),
        note=request.POST.get("note", "")[:200],
        created_by=request.user,
    )
    # A record about somebody belongs in THEIR partition, not in the partition
    # of whoever keyed it in. Only a company-wide holiday, which has no subject,
    # falls back to the person entering it.
    subject = (User.objects.filter(pk=record.user_id)
               .values_list("workspace_id", flat=True).first()
               if record.user_id else None)
    record.workspace_id = (subject if record.user_id
                           else getattr(workspace_for_new(request.user), "pk", None))
    # Whoever may approve leave is recording a decision, not making a request.
    if can_approve_leave(request.user):
        record.status = LeaveRecord.Status.APPROVED
        record.approved_by = request.user
    else:
        record.status = LeaveRecord.Status.REQUESTED

    if record.kind not in LeaveRecord.Kind.values:
        record.kind = LeaveRecord.Kind.OTHER
    if not record.start_date or not record.end_date:
        messages.error(request, "Both dates are required.")
        return redirect("resource_planner:leave")
    try:
        record.full_clean(exclude=["user", "approved_by", "created_by"])
    except ValidationError as error:
        messages.error(request, "; ".join(
            message for messages_ in error.message_dict.values()
            for message in messages_))
        return redirect("resource_planner:leave")

    record.save()
    log_activity(request.user, "recorded leave",
                 str(record.user or "Everyone"),
                 f"{record.get_kind_display()} {record.start_date}–{record.end_date}")
    messages.success(request, "Leave recorded.")
    return redirect("resource_planner:leave")


@login_required
@require_POST
def leave_update(request, pk):
    record = get_object_or_404(
        scope(LeaveRecord.objects, path="user__workspace"), pk=pk)
    action = request.POST.get("action")

    if action == "delete":
        if not can_manage_leave(request.user):
            messages.error(request, "You don't have permission to remove leave.")
            return redirect("resource_planner:leave")
        record.delete()
        log_activity(request.user, "deleted leave", str(record.user or "Everyone"))
        messages.success(request, "Leave removed.")
        return redirect("resource_planner:leave")

    if action in ("approve", "reject"):
        if not can_approve_leave(request.user):
            messages.error(request, "You don't have permission to approve leave.")
            return redirect("resource_planner:leave")
        if record.user_id == request.user.pk:
            # Same rule the HR approvals screen enforces: a decision on your own
            # request is not an approval, and the planner must not be the way
            # around it.
            messages.error(request, "You can't approve your own leave.")
            return redirect("resource_planner:leave")
        record.status = (LeaveRecord.Status.APPROVED if action == "approve"
                         else LeaveRecord.Status.REJECTED)
        record.approved_by = request.user
        record.decided_at = timezone.now()
        record.save(update_fields=["status", "approved_by", "decided_at",
                                   "updated_at"])
        log_activity(request.user, f"{action}d leave", str(record.user or "Everyone"))
        messages.success(request, f"Leave {action}d.")

    return redirect("resource_planner:leave")
