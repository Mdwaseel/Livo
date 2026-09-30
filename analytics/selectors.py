"""Query layer: the only module in this app that touches the ORM.

Everything returns querysets or plain aggregates. No business rules live here —
`services.py` turns these numbers into metrics, `charts.py` turns them into
Chart.js payloads. Keeping the ORM in one file is what makes it possible to
reason about the query count, and it is the seam a materialised view would slot
into later.

House rules, enforced by `analytics/tests/test_performance.py`:

* One aggregate per question. `Sum` over two different joins on one queryset
  double-counts through the join fan-out — the existing project list already
  learned that the hard way — so multi-metric rollups use `filter=Q(...)`
  inside a single pass or separate grouped queries, never stacked joins.
* Anything a template will iterate carries `select_related` for its FKs.
* No queryset is evaluated in a loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from accounts.models import Department
from clients.access import visible_clients
from clients.models import Client
from core.tenancy import scope
from documents.models import Document
from employees.models import EmployeeProfile
from finance.models import Payment
from projects.models import Milestone, Project, Task, WorkLogEntry

ZERO = Decimal("0")
# Coalesce templates. Sum() over an empty set returns None, which then poisons
# every arithmetic operation downstream; folding to 0 in SQL keeps the Python
# side free of `or 0` noise.
_MONEY = DecimalField(max_digits=14, decimal_places=2)
_HOURS = DecimalField(max_digits=12, decimal_places=2)


def money_sum(expression, **kwargs):
    return Coalesce(Sum(expression, **kwargs), Value(ZERO), output_field=_MONEY)


def hours_sum(expression, **kwargs):
    return Coalesce(Sum(expression, **kwargs), Value(ZERO), output_field=_HOURS)


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------

DEFAULT_WINDOW_DAYS = 90


@dataclass
class Filters:
    """The five filters the dashboards share, parsed once per request.

    Held as a value object rather than passed around as loose kwargs so that
    adding a sixth filter is one edit here plus one `apply_*` line, not a
    signature change in twenty functions.
    """

    start: date
    end: date
    employee_id: int | None = None
    project_id: int | None = None
    department_id: int | None = None
    client_id: int | None = None
    # Kept for the "reset" affordance and for round-tripping into export URLs.
    raw: dict = field(default_factory=dict)
    # Who is asking. Carried on the value object rather than passed alongside it
    # because EVERY base queryset below has to narrow by workspace, and the one
    # thing worse than threading a parameter through twenty functions is
    # threading it through nineteen of them.
    viewer: object = None

    @classmethod
    def from_request(cls, request):
        today = timezone.localdate()
        params = request.GET
        start = _parse_date(params.get("start")) or today - timedelta(
            days=DEFAULT_WINDOW_DAYS)
        end = _parse_date(params.get("end")) or today
        # A backwards range silently returns nothing, which reads as "we have
        # no data" rather than "you typed the dates the wrong way round".
        if start > end:
            start, end = end, start
        return cls(
            start=start,
            end=end,
            employee_id=_parse_int(params.get("employee")),
            project_id=_parse_int(params.get("project")),
            department_id=_parse_int(params.get("department")),
            client_id=_parse_int(params.get("client")),
            raw={k: v for k, v in params.items() if k != "page" and v},
            viewer=getattr(request, "user", None),
        )

    @property
    def days(self):
        return (self.end - self.start).days + 1

    @property
    def is_filtered(self):
        return any((self.employee_id, self.project_id,
                    self.department_id, self.client_id))

    def querystring(self, **overrides):
        """Round-trip the current filters into a link (used by the export
        buttons, so a download matches what's on screen)."""
        from urllib.parse import urlencode

        params = {
            "start": self.start.isoformat(), "end": self.end.isoformat(),
            "employee": self.employee_id or "", "project": self.project_id or "",
            "department": self.department_id or "", "client": self.client_id or "",
        }
        params.update(overrides)
        return urlencode({k: v for k, v in params.items() if v not in ("", None)})


def _parse_date(raw):
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except (ValueError, AttributeError):
        return None


def _parse_int(raw):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------------------
# base querysets — each applies the filters that make sense for its model
# ---------------------------------------------------------------------------

def work_logs(filters):
    """Work-log entries in range. `hours` is nullable, so callers must sum with
    `hours_sum`, never bare `Sum`."""
    qs = scope(
        WorkLogEntry.objects.filter(date__gte=filters.start, date__lte=filters.end),
        filters.viewer, path="project__workspace")
    if filters.employee_id:
        qs = qs.filter(logged_by_id=filters.employee_id)
    if filters.project_id:
        qs = qs.filter(project_id=filters.project_id)
    if filters.client_id:
        qs = qs.filter(project__client_id=filters.client_id)
    if filters.department_id:
        # A work log has no department of its own; it inherits the logger's.
        # Falling back to the task's department would double-count entries
        # whose logger sits in a different team from the task.
        qs = qs.filter(logged_by__department_id=filters.department_id)
    return qs


def tasks(filters, *, by_completion=False):
    """Tasks, optionally windowed by when they were completed rather than
    created — "tasks completed this month" and "tasks created this month" are
    different questions and the dashboard asks both."""
    qs = scope(Task.objects.all(), filters.viewer, path="project__workspace")
    if by_completion:
        qs = qs.filter(completed_on__gte=filters.start, completed_on__lte=filters.end)
    else:
        qs = qs.filter(created_at__date__gte=filters.start,
                       created_at__date__lte=filters.end)
    if filters.employee_id:
        qs = qs.filter(assignee_id=filters.employee_id)
    if filters.project_id:
        qs = qs.filter(project_id=filters.project_id)
    if filters.client_id:
        qs = qs.filter(project__client_id=filters.client_id)
    if filters.department_id:
        qs = qs.filter(department_id=filters.department_id)
    return qs


def open_tasks(filters):
    """Unfinished tasks, regardless of when they were created — an overdue task
    from last quarter is still overdue today, so this one deliberately ignores
    the date window."""
    qs = scope(Task.objects.exclude(status=Task.Status.DONE),
               filters.viewer, path="project__workspace")
    if filters.employee_id:
        qs = qs.filter(assignee_id=filters.employee_id)
    if filters.project_id:
        qs = qs.filter(project_id=filters.project_id)
    if filters.client_id:
        qs = qs.filter(project__client_id=filters.client_id)
    if filters.department_id:
        qs = qs.filter(department_id=filters.department_id)
    return qs


def payments(filters):
    """Money received in range. Archived projects are excluded to match
    `Client.total_received` and the dashboard's existing rollups — otherwise
    analytics and the project page would quote different revenue."""
    qs = scope(
        Payment.objects.filter(received_on__gte=filters.start,
                               received_on__lte=filters.end,
                               project__is_archived=False),
        filters.viewer, path="project__workspace")
    if filters.project_id:
        qs = qs.filter(project_id=filters.project_id)
    if filters.client_id:
        qs = qs.filter(project__client_id=filters.client_id)
    return qs


def live_projects(filters):
    """Non-archived projects. Not date-windowed: a project's value and
    outstanding balance are current facts, not events in a period."""
    qs = scope(Project.objects.filter(is_archived=False), filters.viewer)
    if filters.project_id:
        qs = qs.filter(pk=filters.project_id)
    if filters.client_id:
        qs = qs.filter(client_id=filters.client_id)
    return qs


def live_clients(filters):
    qs = visible_clients(Client.objects.filter(is_archived=False), filters.viewer)
    if filters.client_id:
        qs = qs.filter(pk=filters.client_id)
    return qs


def employee_profiles(filters):
    """Employees still on the books. Resigned/terminated staff are excluded so
    "average hours per person" isn't diluted by people who left."""
    qs = (EmployeeProfile.objects
          .exclude(status__in=(EmployeeProfile.Status.RESIGNED,
                               EmployeeProfile.Status.TERMINATED))
          .select_related("user", "user__department", "user__designation"))
    if filters.employee_id:
        qs = qs.filter(user_id=filters.employee_id)
    if filters.department_id:
        qs = qs.filter(user__department_id=filters.department_id)
    return qs


# ---------------------------------------------------------------------------
# headline aggregates
# ---------------------------------------------------------------------------

def hours_breakdown(filters):
    """{total, billable, non_billable, approved, pending} in one pass.

    Conditional aggregates rather than five queries: the row set is identical,
    only the predicate differs, so making the database walk it once is both
    faster and immune to the totals disagreeing with their own parts.
    """
    return work_logs(filters).aggregate(
        total=hours_sum("hours"),
        billable=hours_sum("hours", filter=Q(is_billable=True)),
        non_billable=hours_sum("hours", filter=Q(is_billable=False)),
        approved=hours_sum(
            "hours", filter=Q(approval_status=WorkLogEntry.Approval.APPROVED)),
        pending=hours_sum(
            "hours", filter=Q(approval_status=WorkLogEntry.Approval.SUBMITTED)),
        # The *count* of entries awaiting approval, not their hours — the top
        # card asks "how many are queued", and pulling it into this pass saves
        # a second scan of the same rows.
        pending_count=Count(
            "id", filter=Q(approval_status=WorkLogEntry.Approval.SUBMITTED)),
        entries=Count("id"),
    )


def hours_on(day, *, base_filters):
    """Hours logged on a single date, honouring the non-date filters."""
    single_day = Filters(start=day, end=day,
                         employee_id=base_filters.employee_id,
                         project_id=base_filters.project_id,
                         department_id=base_filters.department_id,
                         client_id=base_filters.client_id,
                         viewer=base_filters.viewer)
    return work_logs(single_day).aggregate(h=hours_sum("hours"))["h"]


def pending_worklog_approvals(filters):
    return work_logs(filters).filter(
        approval_status=WorkLogEntry.Approval.SUBMITTED).count()


def task_status_counts(filters):
    """Board-column counts across the whole (unwindowed) task set for the
    current scope — a status chart of only tasks created this week is close to
    meaningless."""
    board = scope(Task.objects.all(), filters.viewer, path="project__workspace")
    if filters.employee_id:
        board = board.filter(assignee_id=filters.employee_id)
    if filters.project_id:
        board = board.filter(project_id=filters.project_id)
    if filters.client_id:
        board = board.filter(project__client_id=filters.client_id)
    if filters.department_id:
        board = board.filter(department_id=filters.department_id)
    today = timezone.localdate()
    return board.aggregate(
        todo=Count("id", filter=Q(status=Task.Status.TODO)),
        in_progress=Count("id", filter=Q(status=Task.Status.IN_PROGRESS)),
        submitted=Count("id", filter=Q(
            approval_status=Task.Approval.SUBMITTED)),
        done=Count("id", filter=Q(status=Task.Status.DONE)),
        overdue=Count("id", filter=Q(due_date__lt=today) & ~Q(status=Task.Status.DONE)),
        total=Count("id"),
    )


def revenue_totals(filters):
    return payments(filters).aggregate(
        received=money_sum("amount"), payment_count=Count("id"))


def portfolio_totals(filters):
    """Agreed value across live projects, and what's been received against them
    over all time (not the filtered window) — outstanding is a running balance,
    so windowing the receipts would overstate it."""
    value = live_projects(filters).aggregate(v=money_sum("budget"))["v"]
    # `live_projects` is already workspace-narrowed, so filtering payments
    # through it inherits the partition without a second scope() call.
    received = Payment.objects.filter(
        project__in=live_projects(filters)
    ).aggregate(r=money_sum("amount"))["r"]
    return {"value": value, "received": received, "outstanding": value - received}


def documents_generated(filters):
    qs = scope(
        Document.objects.filter(is_archived=False,
                                created_at__date__gte=filters.start,
                                created_at__date__lte=filters.end),
        filters.viewer, path="project__workspace")
    if filters.project_id:
        qs = qs.filter(project_id=filters.project_id)
    if filters.client_id:
        qs = qs.filter(project__client_id=filters.client_id)
    return qs


# ---------------------------------------------------------------------------
# per-entity rollups (one query each, annotated — never a loop)
# ---------------------------------------------------------------------------

def employee_rows(filters):
    """One annotated row per employee.

    Three separate grouped queries stitched together in Python rather than one
    query with three joins: `Sum(work_logs.hours)` and `Count(assigned_tasks)`
    on the same queryset multiply each other through the join fan-out. Three
    small `GROUP BY`s are correct and still O(1) in query count.
    """
    profiles = list(employee_profiles(filters))
    user_ids = [p.user_id for p in profiles]
    if not user_ids:
        return []

    log_scope = work_logs(filters).filter(logged_by_id__in=user_ids)
    by_logger = {
        row["logged_by_id"]: row
        for row in log_scope.values("logged_by_id").annotate(
            total_hours=hours_sum("hours"),
            billable=hours_sum("hours", filter=Q(is_billable=True)),
            approved=hours_sum(
                "hours", filter=Q(approval_status=WorkLogEntry.Approval.APPROVED)),
            # Everything that entered review at all — the denominator of the
            # approval rate. Drafts are excluded: nobody has judged them yet, so
            # counting them would read as a rejection.
            reviewed=hours_sum(
                "hours", filter=~Q(approval_status=WorkLogEntry.Approval.NONE)),
            days=Count("date", distinct=True),
        )
    }

    completed_scope = tasks(filters, by_completion=True).filter(
        assignee_id__in=user_ids, status=Task.Status.DONE)
    by_completed = {
        row["assignee_id"]: row
        for row in completed_scope.values("assignee_id").annotate(
            completed=Count("id"))
    }
    # Average cycle time is computed in Python, not SQL. Subtracting a DateField
    # from a truncated DateTimeField is expressible but backend-dependent, and
    # this has to be right on both SQLite and PostgreSQL. One query returning
    # two date columns for a bounded set of completed tasks is cheap and exact.
    cycle_totals = {}
    for assignee_id, created_at, completed_on in completed_scope.values_list(
            "assignee_id", "created_at", "completed_on"):
        if completed_on is None:
            continue
        days = (completed_on - timezone.localtime(created_at).date()).days
        # Same-day completion is 0 days, not negative; a completed_on backdated
        # before creation is data entry, not a negative cycle.
        total, count = cycle_totals.get(assignee_id, (0, 0))
        cycle_totals[assignee_id] = (total + max(0, days), count + 1)

    open_scope = open_tasks(filters).filter(assignee_id__in=user_ids)
    today = timezone.localdate()
    by_open = {
        row["assignee_id"]: row
        for row in open_scope.values("assignee_id").annotate(
            open_count=Count("id"),
            overdue=Count("id", filter=Q(due_date__lt=today)),
        )
    }

    rows = []
    for profile in profiles:
        total_days, cycle_count = cycle_totals.get(profile.user_id, (0, 0))
        rows.append({
            "profile": profile,
            "logs": by_logger.get(profile.user_id, {}),
            "completed": by_completed.get(profile.user_id, {}),
            "open": by_open.get(profile.user_id, {}),
            "cycle_days": (total_days / cycle_count) if cycle_count else None,
        })
    return rows


def project_rows(filters):
    """One annotated row per live project, with the client pre-joined.

    Four grouped queries for the same fan-out reason as `employee_rows`: hours,
    payments, tasks and milestones each live on their own join.
    """
    projects = list(live_projects(filters).select_related("client"))
    project_ids = [p.pk for p in projects]
    if not project_ids:
        return []

    by_hours = {
        row["project_id"]: row
        for row in work_logs(filters).filter(project_id__in=project_ids)
        .values("project_id").annotate(
            actual=hours_sum("hours"),
            billable=hours_sum("hours", filter=Q(is_billable=True)),
        )
    }
    by_payment = {
        row["project_id"]: row
        for row in Payment.objects.filter(project_id__in=project_ids)
        .values("project_id").annotate(received=money_sum("amount"))
    }
    today = timezone.localdate()
    by_task = {
        row["project_id"]: row
        for row in Task.objects.filter(project_id__in=project_ids)
        .values("project_id").annotate(
            total=Count("id"),
            done=Count("id", filter=Q(status=Task.Status.DONE)),
            overdue=Count("id", filter=Q(due_date__lt=today)
                          & ~Q(status=Task.Status.DONE)),
            estimated=hours_sum("estimated_hours"),
        )
    }
    by_milestone = {
        row["project_id"]: row
        for row in Milestone.objects.filter(project_id__in=project_ids)
        .values("project_id").annotate(
            total=Count("id"),
            done=Count("id", filter=Q(status=Milestone.Status.DONE)),
        )
    }

    return [
        {
            "project": project,
            "hours": by_hours.get(project.pk, {}),
            "payment": by_payment.get(project.pk, {}),
            "tasks": by_task.get(project.pk, {}),
            "milestones": by_milestone.get(project.pk, {}),
        }
        for project in projects
    ]


def client_rows(filters):
    """One annotated row per live client."""
    clients = list(live_clients(filters))
    client_ids = [c.pk for c in clients]
    if not client_ids:
        return []

    by_project = {
        row["client_id"]: row
        for row in Project.objects.filter(client_id__in=client_ids,
                                          is_archived=False)
        .values("client_id").annotate(
            projects=Count("id"),
            active=Count("id", filter=Q(status=Project.Status.ACTIVE)),
            value=money_sum("budget"),
        )
    }
    by_payment = {
        row["project__client_id"]: row
        for row in Payment.objects.filter(project__client_id__in=client_ids,
                                          project__is_archived=False)
        .values("project__client_id").annotate(received=money_sum("amount"))
    }
    by_hours = {
        row["project__client_id"]: row
        for row in work_logs(filters).filter(project__client_id__in=client_ids)
        .values("project__client_id").annotate(total_hours=hours_sum("hours"))
    }
    by_document = {
        row["project__client_id"]: row
        for row in documents_generated(filters)
        .filter(project__client_id__in=client_ids)
        .values("project__client_id").annotate(documents=Count("id"))
    }

    return [
        {
            "client": client,
            "projects": by_project.get(client.pk, {}),
            "payment": by_payment.get(client.pk, {}),
            "hours": by_hours.get(client.pk, {}),
            "documents": by_document.get(client.pk, {}),
        }
        for client in clients
    ]


def department_rows(filters):
    """Hours and completed tasks per department, for the productivity chart."""
    departments = list(Department.objects.all())
    if filters.department_id:
        departments = [d for d in departments if d.pk == filters.department_id]
    if not departments:
        return []
    dept_ids = [d.pk for d in departments]

    by_hours = {
        row["logged_by__department_id"]: row
        for row in work_logs(filters)
        .filter(logged_by__department_id__in=dept_ids)
        .values("logged_by__department_id").annotate(
            total_hours=hours_sum("hours"),
            billable=hours_sum("hours", filter=Q(is_billable=True)),
        )
    }
    by_task = {
        row["department_id"]: row
        for row in tasks(filters, by_completion=True)
        .filter(department_id__in=dept_ids, status=Task.Status.DONE)
        .values("department_id").annotate(completed=Count("id"))
    }
    by_headcount = {
        row["department_id"]: row["people"]
        for row in employee_profiles(filters)
        .filter(user__department_id__in=dept_ids)
        .values(department_id=F("user__department_id")).annotate(people=Count("id"))
    }

    return [
        {
            "department": department,
            "hours": by_hours.get(department.pk, {}),
            "tasks": by_task.get(department.pk, {}),
            "headcount": by_headcount.get(department.pk, 0),
        }
        for department in departments
    ]


# ---------------------------------------------------------------------------
# time series (grouped in SQL, gap-filled in Python)
# ---------------------------------------------------------------------------

def daily_hours_series(filters):
    """[(date, billable, non_billable)] with no gaps.

    Gap-filling matters: a line chart that skips empty days draws a straight
    line across a fortnight of leave and reads as steady work.
    """
    rows = {
        row["date"]: row
        for row in work_logs(filters).values("date").annotate(
            billable=hours_sum("hours", filter=Q(is_billable=True)),
            non_billable=hours_sum("hours", filter=Q(is_billable=False)),
        )
    }
    series, cursor = [], filters.start
    while cursor <= filters.end:
        row = rows.get(cursor)
        series.append((
            cursor,
            row["billable"] if row else ZERO,
            row["non_billable"] if row else ZERO,
        ))
        cursor += timedelta(days=1)
    return series


def monthly_revenue_series(filters):
    """[(first_of_month, amount, count)] with no gaps."""
    rows = {
        row["month"].date() if hasattr(row["month"], "date") else row["month"]: row
        for row in payments(filters)
        .annotate(month=TruncMonth("received_on"))
        .values("month").annotate(amount=money_sum("amount"), count=Count("id"))
    }
    series = []
    for month_start in _months_between(filters.start, filters.end):
        row = rows.get(month_start)
        series.append((month_start,
                       row["amount"] if row else ZERO,
                       row["count"] if row else 0))
    return series


def monthly_growth_series(filters):
    """[(month, revenue, hours, completed_tasks)] — the three growth signals on
    one axis, each gap-filled."""
    revenue = {m: amount for m, amount, _ in monthly_revenue_series(filters)}

    hour_rows = {
        _as_date(row["month"]): row["total_hours"]
        for row in work_logs(filters)
        .annotate(month=TruncMonth("date"))
        .values("month").annotate(total_hours=hours_sum("hours"))
    }
    task_rows = {
        _as_date(row["month"]): row["completed"]
        for row in tasks(filters, by_completion=True)
        .filter(status=Task.Status.DONE)
        .annotate(month=TruncMonth("completed_on"))
        .values("month").annotate(completed=Count("id"))
    }
    return [
        (month, revenue.get(month, ZERO), hour_rows.get(month, ZERO),
         task_rows.get(month, 0))
        for month in _months_between(filters.start, filters.end)
    ]


def _as_date(value):
    return value.date() if hasattr(value, "date") else value


def _months_between(start, end):
    months, cursor = [], start.replace(day=1)
    last = end.replace(day=1)
    while cursor <= last:
        months.append(cursor)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


# ---------------------------------------------------------------------------
# filter dropdown options
# ---------------------------------------------------------------------------

def filter_options(viewer=None):
    """Everything the filter bar needs, in four queries.

    `only()` because the selects need an id and a label and nothing else — and
    the employee list would otherwise drag salary and bank columns into a page
    that has no business holding them.

    The project and client selects are workspace-narrowed. A dropdown is a
    disclosure like any other: an option naming a client the viewer may not
    query is still telling them that client exists. Employees and departments
    are org-wide by design — the directory is shared across workspaces.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return {
        "employees": (User.objects.filter(is_active=True)
                      .only("id", "first_name", "last_name", "username")
                      .order_by("first_name", "username")),
        "projects": (scope(Project.objects.filter(is_archived=False), viewer)
                     .select_related("client")
                     .only("id", "name", "client__name")
                     .order_by("client__name", "name")),
        "departments": Department.objects.only("id", "name").order_by("name"),
        "clients": (visible_clients(
                        Client.objects.filter(is_archived=False), viewer)
                    .only("id", "name").order_by("name")),
    }


def headcount(filters):
    return employee_profiles(filters).count()


def active_project_count(filters):
    return live_projects(filters).filter(status=Project.Status.ACTIVE).count()


def client_count(filters):
    return live_clients(filters).count()


# ---------------------------------------------------------------------------
# executive rollups
#
# Two questions the per-module dashboards never ask, because they are only
# interesting side by side: what is in the pipeline, and is the board filling
# up faster than it empties. Both live here rather than in `services` for the
# same reason as everything above — they touch the ORM.
# ---------------------------------------------------------------------------

# The stages the executive pipeline reports on, in the order work moves through
# them. CANCELLED is deliberately absent: it is not a stage, it is an exit, and
# a funnel that includes it invites reading the drop-off as attrition between
# steps rather than as a decision.
PIPELINE_STAGES = [
    Project.Status.LEAD,
    Project.Status.PROPOSAL,
    Project.Status.ACTIVE,
    Project.Status.ON_HOLD,
    Project.Status.COMPLETED,
]
# Which of those count as still-winnable money. ON_HOLD is in, because a paused
# project is stalled rather than lost and pretending otherwise flatters the
# number; COMPLETED is out, because it has already been won and belongs to
# revenue, not to forecast.
OPEN_STAGES = [
    Project.Status.LEAD,
    Project.Status.PROPOSAL,
    Project.Status.ACTIVE,
    Project.Status.ON_HOLD,
]


def pipeline_by_stage(filters):
    """[(status, label, count, value)] for every stage, zeros included.

    One GROUP BY over the non-archived projects the viewer can see. Empty
    stages are emitted rather than skipped for the reason `core._counts_by`
    already documents: a funnel with nothing in Proposal Sent is a fact about
    the business, and a missing bar hides it.

    `value` sums `budget`, which is nullable — a project logged before anyone
    agreed a number contributes 0 to the value and 1 to the count, so the two
    columns disagree on purpose and the template shows both.
    """
    tally = {
        row["status"]: row
        for row in (live_projects(filters)
                    .values("status")
                    .annotate(count=Count("id"), value=money_sum("budget")))
    }
    labels = dict(Project.Status.choices)
    return [
        (status,
         labels[status],
         tally.get(status, {}).get("count", 0),
         tally.get(status, {}).get("value", ZERO))
        for status in PIPELINE_STAGES
    ]


def open_pipeline_totals(filters):
    """{count, value} for the stages that are still winnable."""
    return (live_projects(filters)
            .filter(status__in=OPEN_STAGES)
            .aggregate(count=Count("id"), value=money_sum("budget")))


def weekly_task_flow(filters):
    """[(week_start, created, completed, backlog)] — is the board filling up?

    `backlog` is the running net of the two inside the window, not the true
    open-task count: the real backlog includes everything opened before the
    window began, which this deliberately does not try to reconstruct. It is
    the *direction* that carries the meaning — a line trending up means the
    team is falling behind over the period on screen — so the series starts at
    zero and the template labels it as a movement rather than a level.

    Two grouped queries, not one per week. `created_at` is a datetime and
    `completed_on` a date, so they are bucketed separately and zipped in
    Python over a gap-filled week index.
    """
    weeks = _weeks_between(filters.start, filters.end)
    if not weeks:
        return []
    index = {week: [0, 0] for week in weeks}

    for row in tasks(filters).values("created_at__date"):
        bucket = _week_start(row["created_at__date"])
        if bucket in index:
            index[bucket][0] += 1
    for row in tasks(filters, by_completion=True).values("completed_on"):
        bucket = _week_start(row["completed_on"])
        if bucket in index:
            index[bucket][1] += 1

    series, backlog = [], 0
    for week in weeks:
        created, completed = index[week]
        backlog += created - completed
        series.append((week, created, completed, backlog))
    return series


def monthly_hours_series(filters):
    """[(month, total, billable)] gap-filled — the people-side trend.

    Separate from `monthly_growth_series` rather than a slice of it because
    that one starts by computing revenue, and this is rendered for viewers who
    hold no `finance.view`. Asking for hours should not run a payments query.
    """
    rows = {
        _as_date(row["month"]): row
        for row in (work_logs(filters)
                    .annotate(month=TruncMonth("date"))
                    .values("month")
                    .annotate(total=hours_sum("hours"),
                              billable=hours_sum("hours",
                                                 filter=Q(is_billable=True))))
    }
    series = []
    for month in _months_between(filters.start, filters.end):
        row = rows.get(month)
        series.append((month,
                       row["total"] if row else ZERO,
                       row["billable"] if row else ZERO))
    return series


def _week_start(value):
    """The Monday of the week `value` falls in."""
    return value - timedelta(days=value.weekday())


def _weeks_between(start, end):
    """Every Monday from the week containing `start` to the week containing
    `end`. Capped so a hand-typed decade-wide range cannot render 500 bars."""
    week = _week_start(start)
    last = _week_start(end)
    weeks = []
    while week <= last and len(weeks) < MAX_FLOW_WEEKS:
        weeks.append(week)
        week += timedelta(days=7)
    return weeks


# A year and a bit of weekly bars is already more than a chart can say
# usefully; past that the axis is unreadable and the query grows without
# telling anyone anything new.
MAX_FLOW_WEEKS = 60
