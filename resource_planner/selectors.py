"""Query layer. The only module here that touches the ORM.

Reuses the existing work-log service rather than re-deriving logged hours:
`analytics.selectors.hours_sum` and its `work_logs()` scoping are imported
directly, so "hours logged" means exactly the same thing on the planner as it
does on the analytics dashboard. The dependency runs one way
(resource_planner → analytics) and there is no cycle.

**How allocation is defined**, because this is the one judgement the whole
module rests on:

    allocated = hours already logged in the period
              + REMAINING estimate of open tasks due in the period

Remaining, not full, estimate — `Task.actual_hours` is rolled up from work logs
by `Task.sync_actual_hours`, so counting the whole estimate alongside logged
hours would bill the same work twice. A task estimated at 8 h with 5 h logged
contributes 5 h of logged time plus 3 h of outstanding work: 8 h total, once.

Open tasks whose due date has already passed keep counting against the current
period. They are still work somebody has to do, and quietly dropping them is
how a planner shows a comfortable 60% for a person who is three weeks behind.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Case, Count, DecimalField, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce, Greatest

from accounts.models import Department
from analytics.selectors import hours_sum
from core.tenancy import scope
from employees.models import EmployeeProfile
from projects.models import Project, Sprint, Task, WorkLogEntry

from .models import CapacityProfile, LeaveRecord

User = get_user_model()
ZERO = Decimal("0")
_HOURS = DecimalField(max_digits=12, decimal_places=2)

# `estimated_hours - actual_hours`, floored at zero: a task that overran its
# estimate has no *remaining* planned work, and a negative would credit the
# person with free time they haven't got.
REMAINING_ESTIMATE = Greatest(
    Coalesce(F("estimated_hours"), Value(ZERO), output_field=_HOURS)
    - Coalesce(F("actual_hours"), Value(ZERO), output_field=_HOURS),
    Value(ZERO),
    output_field=_HOURS,
)


# ---------------------------------------------------------------------------
# people in scope
# ---------------------------------------------------------------------------

def plannable_users(*, department_id=None, user_id=None):
    """Active staff the planner schedules.

    Driven off `EmployeeProfile` so the same resigned/terminated exclusion the
    analytics module uses applies here too — planning capacity for somebody who
    left is worse than useless, it inflates the agency's headline availability.
    """
    profiles = (EmployeeProfile.objects
                .exclude(status__in=(EmployeeProfile.Status.RESIGNED,
                                     EmployeeProfile.Status.TERMINATED))
                .filter(user__is_active=True)
                .select_related("user", "user__department", "user__designation"))
    if department_id:
        profiles = profiles.filter(user__department_id=department_id)
    if user_id:
        profiles = profiles.filter(user_id=user_id)
    return profiles.order_by("user__first_name", "user__username")


def capacity_profiles(users):
    """{user_id: CapacityProfile} in one query, defaults filled in."""
    return CapacityProfile.map_for(users)


def leave_records(start, end, *, user_ids=None):
    """Every leave row overlapping the window, personal and company-wide.

    One query feeds the whole `LeaveCalendar`; the planner never asks "is this
    person off today" per cell.
    """
    overlapping = Q(start_date__lte=end, end_date__gte=start)
    # Named `whose` rather than `scope`: the module imports a `scope()` helper
    # from core.tenancy, and a local of that name would shadow it here.
    whose = Q(user__isnull=True)  # company-wide always applies
    if user_ids is None:
        whose |= Q(user__isnull=False)
    else:
        whose |= Q(user_id__in=list(user_ids))
    return (scope(LeaveRecord.objects, path="user__workspace")
            .filter(overlapping)
            .filter(whose)
            .exclude(status__in=(LeaveRecord.Status.REJECTED,
                                 LeaveRecord.Status.CANCELLED))
            .select_related("user"))


# ---------------------------------------------------------------------------
# logged hours (reusing the work-log service)
# ---------------------------------------------------------------------------

def logged_hours_by_user(start, end, *, user_ids=None, project_id=None):
    """{user_id: hours} — one grouped query.

    Workspace-narrowed like every other number on the planner. The viewer comes
    from the request scope parked by `core.tenancy.WorkspaceMiddleware` rather
    than a parameter: these selectors are already called with six keyword
    arguments each, and the answer to "whose hours" is the same for all of them.
    """
    queryset = scope(
        WorkLogEntry.objects.filter(date__gte=start, date__lte=end),
        path="project__workspace")
    if user_ids is not None:
        queryset = queryset.filter(logged_by_id__in=list(user_ids))
    if project_id:
        queryset = queryset.filter(project_id=project_id)
    return {row["logged_by_id"]: row["total"]
            for row in queryset.values("logged_by_id")
            .annotate(total=hours_sum("hours"))
            if row["logged_by_id"] is not None}


def logged_hours_by_user_day(start, end, *, user_ids=None):
    """{(user_id, date): hours} — the heatmap's raw material, one query."""
    queryset = scope(
        WorkLogEntry.objects.filter(date__gte=start, date__lte=end),
        path="project__workspace")
    if user_ids is not None:
        queryset = queryset.filter(logged_by_id__in=list(user_ids))
    return {
        (row["logged_by_id"], row["date"]): row["total"]
        for row in queryset.values("logged_by_id", "date")
        .annotate(total=hours_sum("hours"))
        if row["logged_by_id"] is not None
    }


# ---------------------------------------------------------------------------
# scheduled (not yet done) work
# ---------------------------------------------------------------------------

def open_tasks(*, user_ids=None, project_id=None, department_id=None):
    """Unfinished tasks — the source of every forward-looking number."""
    queryset = scope(Task.objects.exclude(status=Task.Status.DONE),
                     path="project__workspace")
    if user_ids is not None:
        queryset = queryset.filter(assignee_id__in=list(user_ids))
    if project_id:
        queryset = queryset.filter(project_id=project_id)
    if department_id:
        queryset = queryset.filter(department_id=department_id)
    return queryset


def scheduled_hours_by_user(start, end, *, user_ids=None, project_id=None,
                            include_overdue=True):
    """{user_id: remaining estimate} for open tasks landing in the window.

    `include_overdue` folds in open tasks that were due before the window
    started — work that is late is still work, and it has to appear on
    somebody's plate or the planner lies by omission.
    """
    queryset = open_tasks(user_ids=user_ids, project_id=project_id)
    window = Q(due_date__gte=start, due_date__lte=end)
    if include_overdue:
        window |= Q(due_date__lt=start)
    queryset = queryset.filter(window)
    return {
        row["assignee_id"]: row["total"]
        for row in queryset.values("assignee_id")
        .annotate(total=Coalesce(Sum(REMAINING_ESTIMATE), Value(ZERO),
                                 output_field=_HOURS))
        if row["assignee_id"] is not None
    }


def scheduled_hours_by_user_day(start, end, *, user_ids=None):
    """{(user_id, due_date): remaining estimate} for the heatmap.

    A task's remaining effort is charged in full to its due date rather than
    spread across the days before it. Spreading would need a start date the
    model does not have, and inventing one would make the grid look precise
    while being made up. The due-date view answers the question the planner is
    actually asked: what lands on this person on this day.
    """
    queryset = (open_tasks(user_ids=user_ids)
                .filter(due_date__gte=start, due_date__lte=end))
    return {
        (row["assignee_id"], row["due_date"]): row["total"]
        for row in queryset.values("assignee_id", "due_date")
        .annotate(total=Coalesce(Sum(REMAINING_ESTIMATE), Value(ZERO),
                                 output_field=_HOURS))
        if row["assignee_id"] is not None
    }


def future_scheduled_by_user(today, horizon_days=90, *, user_ids=None):
    """Open work due after today — "what's coming", separate from what's late."""
    return scheduled_hours_by_user(
        today + timedelta(days=1), today + timedelta(days=horizon_days),
        user_ids=user_ids, include_overdue=False)


def overdue_hours_by_user(today, *, user_ids=None):
    queryset = (open_tasks(user_ids=user_ids)
                .filter(due_date__lt=today))
    return {
        row["assignee_id"]: row["total"]
        for row in queryset.values("assignee_id")
        .annotate(total=Coalesce(Sum(REMAINING_ESTIMATE), Value(ZERO),
                                 output_field=_HOURS))
        if row["assignee_id"] is not None
    }


def sprint_hours_by_user(today, *, user_ids=None):
    """Remaining hours in sprints that are running right now.

    "Current sprint" means active *and* covering today. `is_active` alone would
    include a sprint somebody forgot to close in March.
    """
    running = Sprint.objects.filter(is_active=True).filter(
        Q(start_date__isnull=True) | Q(start_date__lte=today)).filter(
        Q(end_date__isnull=True) | Q(end_date__gte=today))
    queryset = open_tasks(user_ids=user_ids).filter(sprint__in=running)
    return {
        row["assignee_id"]: row["total"]
        for row in queryset.values("assignee_id")
        .annotate(total=Coalesce(Sum(REMAINING_ESTIMATE), Value(ZERO),
                                 output_field=_HOURS))
        if row["assignee_id"] is not None
    }


def task_counts_by_user(*, user_ids=None):
    """{user_id: {open, unestimated}} — an unestimated open task is invisible to
    every hours calculation, so the count travels with the numbers to stop a
    reassuring 40% hiding fifteen unsized jobs."""
    queryset = open_tasks(user_ids=user_ids)
    return {
        row["assignee_id"]: row
        for row in queryset.values("assignee_id").annotate(
            open_count=Count("id"),
            unestimated=Count("id", filter=Q(estimated_hours__isnull=True)),
        )
        if row["assignee_id"] is not None
    }


def unassigned_open_tasks(*, project_id=None, limit=50):
    """The backlog with nobody's name on it — the pool the planner drags from."""
    queryset = (scope(Task.objects.exclude(status=Task.Status.DONE),
                      path="project__workspace")
                .filter(assignee__isnull=True)
                .select_related("project", "project__client"))
    if project_id:
        queryset = queryset.filter(project_id=project_id)
    return queryset.order_by("due_date", "-priority")[:limit]


def planner_tasks(start, end, *, user_ids=None):
    """Open tasks with a due date in the window, ready to render as draggable
    chips. `select_related` because every chip prints its project name."""
    return (open_tasks(user_ids=user_ids)
            .filter(due_date__gte=start, due_date__lte=end)
            .select_related("project", "project__client", "assignee")
            .order_by("due_date", "-priority", "id"))


# ---------------------------------------------------------------------------
# project + department scope
# ---------------------------------------------------------------------------

def project_allocation_rows(start, end, *, project_id=None):
    """Per project: who is on it and for how many hours, in three queries."""
    projects = scope(Project.objects.filter(is_archived=False)).select_related(
        "client")
    if project_id:
        projects = projects.filter(pk=project_id)
    projects = list(projects)
    project_ids = [project.pk for project in projects]
    if not project_ids:
        return []

    logged = {
        (row["project_id"], row["logged_by_id"]): row["total"]
        for row in WorkLogEntry.objects
        .filter(project_id__in=project_ids, date__gte=start, date__lte=end)
        .values("project_id", "logged_by_id").annotate(total=hours_sum("hours"))
    }
    scheduled = {
        (row["project_id"], row["assignee_id"]): row["total"]
        for row in Task.objects
        .filter(project_id__in=project_ids).exclude(status=Task.Status.DONE)
        .values("project_id", "assignee_id")
        .annotate(total=Coalesce(Sum(REMAINING_ESTIMATE), Value(ZERO),
                                 output_field=_HOURS))
    }
    people = {user.pk: user for user in User.objects.filter(
        Q(pk__in={key[1] for key in logged if key[1]})
        | Q(pk__in={key[1] for key in scheduled if key[1]}))
        .select_related("department")}

    rows = []
    for project in projects:
        members = {}
        for (pid, uid), hours in logged.items():
            if pid == project.pk and uid:
                members.setdefault(uid, {"logged": ZERO, "scheduled": ZERO})
                members[uid]["logged"] = hours
        for (pid, uid), hours in scheduled.items():
            if pid == project.pk and uid:
                members.setdefault(uid, {"logged": ZERO, "scheduled": ZERO})
                members[uid]["scheduled"] = hours
        rows.append({
            "project": project,
            "members": [
                {"user": people[uid], **values}
                for uid, values in members.items() if uid in people
            ],
        })
    return rows


def departments():
    return Department.objects.all().order_by("name")
