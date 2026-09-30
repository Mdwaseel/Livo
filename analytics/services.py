"""Metrics: turn the raw aggregates from `selectors` into the numbers a person
reads.

Nothing here touches the ORM, and nothing here re-implements a rule that
already exists elsewhere. Where the rest of the app has already decided what a
thing means, this module reuses that decision:

* "outstanding" is `budget − received`, matching `Project.outstanding` and
  `Client.outstanding`.
* "overdue" is `due_date < today and status != DONE`, matching
  `Task.is_overdue`.
* archived projects are excluded from money, matching the existing dashboard.

The genuinely new judgements are the composite scores — utilisation, risk,
health — and each one carries its formula and its reasoning in a comment,
because a score nobody can explain is a score nobody should act on.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from projects.models import Project, Task

from . import selectors

ZERO = Decimal("0")

# A full-time day. Utilisation is hours ÷ (working days × this), so it is the
# one number that decides whether 6 h/day reads as 75% or 100%. Kept as a module
# constant so it can move to AgencySettings later without touching the maths.
STANDARD_DAY_HOURS = Decimal("8")
WORKING_DAYS_PER_WEEK = 5


def _q(value, places="0.01"):
    """Decimal rounding that matches how the figure is displayed, so a total
    and its parts can't disagree by a rounding cent."""
    if value is None:
        return None
    return Decimal(value).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _percent(part, whole):
    """0–100, one decimal. Returns 0.0 rather than None for an empty
    denominator: a bar chart needs a number, and "no data" is rendered by the
    surrounding empty state, not by a null slipping into the axis."""
    part = Decimal(part or 0)
    whole = Decimal(whole or 0)
    if whole <= 0:
        return 0.0
    return float(_q(part * 100 / whole, "0.1"))


def working_days(start, end):
    """Mon–Fri count in the window. Utilisation divided by calendar days would
    punish everyone by 2/7ths for not working weekends."""
    days = 0
    cursor = start
    while cursor <= end:
        if cursor.weekday() < WORKING_DAYS_PER_WEEK:
            days += 1
        cursor += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# 1. top cards
# ---------------------------------------------------------------------------

def overview_cards(filters, *, include_finance=True, hours=None,
                   task_counts=None):
    """The headline row. `include_finance=False` drops the money cards for
    users without `finance.view` — the same gate the dashboard already uses.

    `hours` and `task_counts` can be passed in by a caller that already has
    them. The dashboard needs both for its charts anyway, and recomputing them
    here cost two extra aggregate queries on every page load.
    """
    today = timezone.localdate()
    if hours is None:
        hours = selectors.hours_breakdown(filters)
    if task_counts is None:
        task_counts = selectors.task_status_counts(filters)
    completed = selectors.tasks(filters, by_completion=True).filter(
        status=Task.Status.DONE).count()
    # Already counted inside hours_breakdown's single pass.
    pending_approvals = hours.get("pending_count", 0)

    cards = [
        _card("Clients", selectors.client_count(filters), "count"),
        _card("Active projects", selectors.active_project_count(filters), "count"),
        _card("Employees", selectors.headcount(filters), "count"),
        _card("Tasks due today",
              selectors.open_tasks(filters).filter(due_date=today).count(),
              "count",
              tone="warn" if task_counts["overdue"] else None),
        _card("Tasks completed", completed, "count"),
        _card("Hours logged today",
              selectors.hours_on(today, base_filters=filters), "hours"),
        _card("Billable hours", hours["billable"], "hours"),
        _card("Non-billable hours", hours["non_billable"], "hours"),
        _card("Pending approvals", pending_approvals, "count",
              tone="warn" if pending_approvals else None),
    ]
    if include_finance:
        revenue = selectors.revenue_totals(filters)
        portfolio = selectors.portfolio_totals(filters)
        outstanding = portfolio["outstanding"]
        cards.extend([
            _card("Revenue", revenue["received"], "money"),
            # Negative outstanding means the client has paid ahead. Showing
            # "₹-25,000 outstanding" reads as a rendering fault, so it is
            # relabelled — the same treatment core.views.dashboard gives it.
            _card("Client credit" if outstanding < 0 else "Outstanding",
                  abs(outstanding), "money",
                  tone="ok" if outstanding < 0 else None),
        ])
    return cards


def _card(label, value, kind, *, tone=None):
    return {"label": label, "value": value, "kind": kind, "tone": tone}


# ---------------------------------------------------------------------------
# 2. employee analytics
# ---------------------------------------------------------------------------

def employee_metrics(filters):
    """One row per employee, sorted by hours logged (descending).

    Reads only what `selectors.employee_rows` already fetched — no further
    queries, no per-row property access that would trigger one.
    """
    available_days = working_days(filters.start, filters.end)
    capacity = Decimal(available_days) * STANDARD_DAY_HOURS
    weeks = Decimal(max(1, available_days)) / Decimal(WORKING_DAYS_PER_WEEK)

    rows = []
    for row in selectors.employee_rows(filters):
        logs = row["logs"]
        total = logs.get("total_hours") or ZERO
        billable = logs.get("billable") or ZERO
        approved = logs.get("approved") or ZERO
        reviewed = logs.get("reviewed") or ZERO
        logged_days = logs.get("days") or 0

        rows.append({
            "user": row["profile"].user,
            "profile": row["profile"],
            "department": row["profile"].user.department,
            "total_hours": _q(total),
            # Averaged over days actually worked, not over the window: someone
            # who logged 8 h on their two days in the office averages 8, not 0.6.
            "avg_daily_hours": _q(total / logged_days) if logged_days else None,
            "avg_weekly_hours": _q(total / weeks) if weeks else None,
            "tasks_completed": row["completed"].get("completed") or 0,
            "avg_completion_days": (round(row["cycle_days"], 1)
                                    if row["cycle_days"] is not None else None),
            "billable_percent": _percent(billable, total),
            # Against a full-time norm for the window. Over 100% is real and is
            # shown as-is — capping it would hide exactly the overwork this
            # number exists to surface.
            "utilization_percent": _percent(total, capacity),
            "approval_percent": _percent(approved, reviewed),
            "open_tasks": row["open"].get("open_count") or 0,
            "overdue_tasks": row["open"].get("overdue") or 0,
        })
    rows.sort(key=lambda r: r["total_hours"] or ZERO, reverse=True)
    return rows


def productivity_trend(filters):
    """Company-wide daily hours, split billable/non-billable. Per-employee
    trends come from the same series with the employee filter applied, so there
    is only one code path to keep honest."""
    return selectors.daily_hours_series(filters)


# ---------------------------------------------------------------------------
# 3. project analytics
# ---------------------------------------------------------------------------

# Risk thresholds. Named rather than inlined so the table legend and the
# calculation can never drift apart.
RISK_OVERRUN = 110      # actual hours as % of estimate
RISK_OVERDUE_TASKS = 3


def project_metrics(filters, *, include_finance=True):
    rows = []
    for row in selectors.project_rows(filters):
        project = row["project"]
        task_stats = row["tasks"]
        milestone_stats = row["milestones"]

        estimated = task_stats.get("estimated") or ZERO
        actual = row["hours"].get("actual") or ZERO
        total_tasks = task_stats.get("total") or 0
        done_tasks = task_stats.get("done") or 0
        overdue = task_stats.get("overdue") or 0
        milestones_total = milestone_stats.get("total") or 0
        milestones_done = milestone_stats.get("done") or 0

        budget = project.budget or ZERO
        received = row["payment"].get("received") or ZERO
        # Mirrors Project.outstanding — the property is not called here because
        # doing so would fire a query per row.
        outstanding = budget - received

        completion = _percent(done_tasks, total_tasks)
        milestone_progress = _percent(milestones_done, milestones_total)
        effort_ratio = _percent(actual, estimated) if estimated else 0.0

        metric = {
            "project": project,
            "client": project.client,
            "estimated_hours": _q(estimated),
            "actual_hours": _q(actual),
            "hours_variance": _q(actual - estimated),
            "effort_percent": effort_ratio,
            "completion_percent": completion,
            "milestone_progress": milestone_progress,
            "milestones_done": milestones_done,
            "milestones_total": milestones_total,
            "open_tasks": total_tasks - done_tasks,
            "overdue_tasks": overdue,
            "is_overdue": _is_past_target(project),
        }
        if include_finance:
            metric.update({
                "budget": _q(budget),
                "received": _q(received),
                "outstanding": _q(outstanding),
                "collection_percent": _percent(received, budget),
            })
        metric["risk"] = project_risk(metric)
        metric["health"] = project_health_score(metric,
                                                include_finance=include_finance)
        rows.append(metric)

    rows.sort(key=lambda r: r["health"])
    return rows


def _is_past_target(project):
    return bool(project.target_end_date
                and project.target_end_date < timezone.localdate()
                and project.status not in (Project.Status.COMPLETED,
                                           Project.Status.CANCELLED))


def project_risk(metric):
    """LOW / MEDIUM / HIGH from three independent signals.

    Counting signals rather than blending them into a weighted number keeps the
    label explainable: a project is high risk *because* it is past its date and
    over its estimate, and the UI can say so.
    """
    signals = []
    if metric["is_overdue"]:
        signals.append("past target date")
    if metric["estimated_hours"] and metric["effort_percent"] > RISK_OVERRUN:
        signals.append("over effort estimate")
    if metric["overdue_tasks"] >= RISK_OVERDUE_TASKS:
        signals.append(f"{metric['overdue_tasks']} overdue tasks")
    # Money is a risk signal only when we were given a budget to judge against.
    if metric.get("budget") and metric.get("collection_percent", 0) < 25 \
            and metric["completion_percent"] > 60:
        signals.append("delivery ahead of collection")

    level = "HIGH" if len(signals) >= 2 else "MEDIUM" if signals else "LOW"
    return {"level": level, "reasons": signals}


def project_health_score(metric, *, include_finance=True):
    """0–100. Higher is healthier.

    Starts at 100 and deducts for each thing that is actually wrong, so a
    project with no problems scores 100 and every point lost has a named cause.
    An average of ratios was rejected: it makes a project with no estimates and
    no budget look mediocre rather than simply unmeasured.

    The score is computed from what the *viewer* is allowed to see, so a reader
    without `finance.view` can score the same project higher — the collection
    deduction is invisible to them. That is intended: the alternative is
    deducting points for a reason we then refuse to explain, which is worse
    than a slightly optimistic number. The finance-blind score is therefore
    always ≥ the finance-aware one, never lower.
    """
    score = 100
    if metric["is_overdue"]:
        score -= 25
    # Effort overrun, scaled: 10% over costs 5, 100% over costs the full 25.
    if metric["estimated_hours"] and metric["effort_percent"] > 100:
        overrun = metric["effort_percent"] - 100
        score -= min(25, int(overrun / 4))
    score -= min(20, metric["overdue_tasks"] * 5)
    # A project that is late on delivery *and* has taken no money is the worst
    # combination, so collection only counts once there is a budget to collect.
    if include_finance and metric.get("budget"):
        shortfall = metric["completion_percent"] - metric.get("collection_percent", 0)
        if shortfall > 25:
            score -= min(15, int(shortfall / 5))
    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# 4. finance analytics
# ---------------------------------------------------------------------------

def finance_metrics(filters):
    revenue = selectors.revenue_totals(filters)
    portfolio = selectors.portfolio_totals(filters)
    project_count = selectors.live_projects(filters).count()
    client_total = selectors.client_count(filters)

    received = revenue["received"]
    value = portfolio["value"]
    return {
        "revenue": _q(received),
        "payment_count": revenue["payment_count"],
        "portfolio_value": _q(value),
        "collected_all_time": _q(portfolio["received"]),
        "outstanding": _q(portfolio["outstanding"]),
        # What share of everything we have agreed to bill has actually landed.
        "collection_percent": _percent(portfolio["received"], value),
        "avg_project_value": _q(value / project_count) if project_count else ZERO,
        "avg_client_value": _q(value / client_total) if client_total else ZERO,
        "avg_payment": _q(received / revenue["payment_count"])
                       if revenue["payment_count"] else ZERO,
        "trend": selectors.monthly_revenue_series(filters),
    }


# ---------------------------------------------------------------------------
# 5. client analytics
# ---------------------------------------------------------------------------

def client_metrics(filters, *, include_finance=True):
    rows = []
    for row in selectors.client_rows(filters):
        client = row["client"]
        value = row["projects"].get("value") or ZERO
        received = row["payment"].get("received") or ZERO

        metric = {
            "client": client,
            "project_count": row["projects"].get("projects") or 0,
            "active_projects": row["projects"].get("active") or 0,
            "hours": _q(row["hours"].get("total_hours") or ZERO),
            "documents": row["documents"].get("documents") or 0,
        }
        if include_finance:
            metric.update({
                "value": _q(value),
                "revenue": _q(received),
                "outstanding": _q(value - received),
                "collection_percent": _percent(received, value),
            })
        metric["health"] = client_health_score(metric,
                                               include_finance=include_finance)
        rows.append(metric)

    rows.sort(key=lambda r: r.get("revenue") or r["hours"] or ZERO, reverse=True)
    return rows


def client_health_score(metric, *, include_finance=True):
    """0–100 for the relationship, not the money.

    Engagement (do they have live work?) and payment behaviour, weighted so
    that a client who pays on time but has nothing running still shows as
    at-risk — that is a renewal conversation, and it is the thing a revenue
    figure alone hides.
    """
    score = 50 if metric["active_projects"] else 20
    if metric["hours"] > 0:
        score += 15
    if metric["documents"] > 0:
        score += 5
    if include_finance:
        score += int(metric.get("collection_percent", 0) * 0.30)
    else:
        # Without finance visibility the money component can't be scored, so
        # spread its weight over what is visible rather than scoring everyone
        # 30 points lower than a colleague looking at the same client.
        score += 30 if metric["active_projects"] else 0
    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# 6. department analytics
# ---------------------------------------------------------------------------

def department_metrics(filters):
    available_days = working_days(filters.start, filters.end)
    rows = []
    for row in selectors.department_rows(filters):
        hours = row["hours"].get("total_hours") or ZERO
        billable = row["hours"].get("billable") or ZERO
        people = row["headcount"] or 0
        capacity = Decimal(available_days) * STANDARD_DAY_HOURS * Decimal(people)
        rows.append({
            "department": row["department"],
            "headcount": people,
            "hours": _q(hours),
            "billable_hours": _q(billable),
            "billable_percent": _percent(billable, hours),
            "tasks_completed": row["tasks"].get("completed") or 0,
            "utilization_percent": _percent(hours, capacity),
        })
    rows.sort(key=lambda r: r["hours"], reverse=True)
    return rows
