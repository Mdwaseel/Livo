"""Workload assembly: capacity from `capacity.py` divided into allocation from
`selectors.py`, turned into the rows and grids the templates render.

No ORM here, and no arithmetic that `capacity.py` already owns. Each public
function takes a `Window` and returns plain dicts, so every number on every
screen can be asserted without a request.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from . import capacity as cap
from . import selectors

ZERO = Decimal("0")
DEFAULT_HORIZON_DAYS = 90


def _q(value):
    return Decimal(value or 0).quantize(Decimal("0.01"))


@dataclass
class Window:
    """The period being planned, plus the scope filters."""
    start: date
    end: date
    department_id: int | None = None
    user_id: int | None = None
    project_id: int | None = None
    mode: str = "week"  # week | month | range

    @classmethod
    def for_week(cls, anchor=None, **scope):
        anchor = anchor or timezone.localdate()
        start = cap.week_start(anchor)
        return cls(start=start, end=start + timedelta(days=6), mode="week", **scope)

    @classmethod
    def for_month(cls, anchor=None, **scope):
        anchor = anchor or timezone.localdate()
        return cls(start=cap.month_start(anchor), end=cap.month_end(anchor),
                   mode="month", **scope)

    @property
    def days(self):
        return list(cap.date_range(self.start, self.end))

    @property
    def previous(self):
        if self.mode == "month":
            return cap.month_start(self.start - timedelta(days=1))
        return self.start - timedelta(days=7)

    @property
    def next(self):
        if self.mode == "month":
            return cap.month_end(self.start) + timedelta(days=1)
        return self.start + timedelta(days=7)


# ---------------------------------------------------------------------------
# the core rollup — every view is built from this
# ---------------------------------------------------------------------------

def workload(window, *, today=None):
    """One row per plannable person, with capacity, allocation and status.

    Fetches everything in a fixed number of queries first, then does all the
    arithmetic in memory. That ordering is the whole performance story: the
    loop below never touches the database, so twenty people cost the same
    number of queries as two.
    """
    today = today or timezone.localdate()

    profiles = list(selectors.plannable_users(
        department_id=window.department_id, user_id=window.user_id))
    users = [profile.user for profile in profiles]
    user_ids = [user.pk for user in users]
    if not user_ids:
        return []

    capacities = selectors.capacity_profiles(users)
    calendar = cap.LeaveCalendar(
        selectors.leave_records(window.start, window.end, user_ids=user_ids))
    logged = selectors.logged_hours_by_user(
        window.start, window.end, user_ids=user_ids, project_id=window.project_id)
    scheduled = selectors.scheduled_hours_by_user(
        window.start, window.end, user_ids=user_ids, project_id=window.project_id)
    future = selectors.future_scheduled_by_user(
        today, DEFAULT_HORIZON_DAYS, user_ids=user_ids)
    overdue = selectors.overdue_hours_by_user(today, user_ids=user_ids)
    sprint = selectors.sprint_hours_by_user(today, user_ids=user_ids)
    counts = selectors.task_counts_by_user(user_ids=user_ids)

    rows = []
    for profile in profiles:
        user = profile.user
        capacity_profile = capacities[user.pk]

        available = cap.capacity_between(
            capacity_profile, window.start, window.end, calendar)
        gross = cap.gross_capacity_between(
            capacity_profile, window.start, window.end)
        logged_hours = _q(logged.get(user.pk))
        scheduled_hours = _q(scheduled.get(user.pk))
        allocated = logged_hours + scheduled_hours
        utilization = cap.utilization_percent(allocated, available)
        task_stats = counts.get(user.pk, {})

        rows.append({
            "user": user,
            "employee": profile,
            "department": user.department,
            "capacity_profile": capacity_profile,
            "daily_hours": _q(capacity_profile.daily_hours),
            "weekly_hours": _q(capacity_profile.weekly_hours),
            "monthly_hours": _q(capacity_profile.monthly_hours),
            "capacity": _q(available),
            "gross_capacity": _q(gross),
            "leave_hours": _q(gross - available),
            "logged_hours": logged_hours,
            "scheduled_hours": scheduled_hours,
            "allocated_hours": _q(allocated),
            # Can go negative — that is the overbooking figure, and clamping it
            # to zero would hide exactly what this module exists to show.
            "remaining_capacity": _q(available - allocated),
            "utilization_percent": utilization,
            "status": cap.status_for(utilization, available),
            "status_label": cap.STATUS_LABELS[cap.status_for(utilization, available)],
            "future_hours": _q(future.get(user.pk)),
            "overdue_hours": _q(overdue.get(user.pk)),
            "sprint_hours": _q(sprint.get(user.pk)),
            "open_tasks": task_stats.get("open_count", 0),
            "unestimated_tasks": task_stats.get("unestimated", 0),
        })

    rows.sort(key=lambda row: row["utilization_percent"], reverse=True)
    return rows


def summarise(rows):
    """Agency-level totals from an already-computed workload list.

    Takes the rows rather than re-querying, so the headline can never disagree
    with the table underneath it.
    """
    capacity_total = sum((row["capacity"] for row in rows), ZERO)
    allocated_total = sum((row["allocated_hours"] for row in rows), ZERO)
    counts = {status: 0 for status in
              (cap.STATUS_AVAILABLE, cap.STATUS_NEAR,
               cap.STATUS_OVER, cap.STATUS_OFF)}
    for row in rows:
        counts[row["status"]] += 1
    utilization = cap.utilization_percent(allocated_total, capacity_total)
    return {
        "people": len(rows),
        "capacity": _q(capacity_total),
        "allocated": _q(allocated_total),
        "remaining": _q(capacity_total - allocated_total),
        "utilization_percent": utilization,
        "status": cap.status_for(utilization, capacity_total),
        "status_label": cap.STATUS_LABELS[cap.status_for(utilization, capacity_total)],
        "available_count": counts[cap.STATUS_AVAILABLE],
        "near_count": counts[cap.STATUS_NEAR],
        "over_count": counts[cap.STATUS_OVER],
        "off_count": counts[cap.STATUS_OFF],
        "leave_hours": _q(sum((row["leave_hours"] for row in rows), ZERO)),
        "unestimated_tasks": sum(row["unestimated_tasks"] for row in rows),
    }


# ---------------------------------------------------------------------------
# who is free / who is drowning
# ---------------------------------------------------------------------------

def availability_board(rows):
    """The three lists a manager actually opens this module for.

    "Can take more" is not simply "available": someone at 20% with fifteen
    unestimated tasks is not safe to load up, so they are held back into the
    'unclear' bucket rather than advertised as free.
    """
    free, near, over, unclear = [], [], [], []
    for row in rows:
        if row["status"] == cap.STATUS_OFF:
            continue
        if row["unestimated_tasks"] and row["status"] == cap.STATUS_AVAILABLE:
            unclear.append(row)
        elif row["status"] == cap.STATUS_AVAILABLE:
            free.append(row)
        elif row["status"] == cap.STATUS_NEAR:
            near.append(row)
        else:
            over.append(row)
    free.sort(key=lambda row: row["remaining_capacity"], reverse=True)
    over.sort(key=lambda row: row["remaining_capacity"])
    return {"free": free, "near": near, "over": over, "unclear": unclear}


def overbooking_report(rows):
    """Everyone past 100%, worst first, with the size of the overrun."""
    flagged = [row for row in rows if row["remaining_capacity"] < 0]
    flagged.sort(key=lambda row: row["remaining_capacity"])
    return {
        "rows": flagged,
        "total_overrun": _q(sum((-row["remaining_capacity"] for row in flagged), ZERO)),
        "count": len(flagged),
    }


def would_overbook(row, extra_hours):
    """Would adding `extra_hours` to this person push them past capacity?

    Used by the drag-and-drop endpoint to warn *after* a successful move rather
    than to block it. Refusing the drop would be wrong — a manager reassigning
    into an overload is often doing it deliberately, and knows something the
    estimate does not.
    """
    projected = row["allocated_hours"] + Decimal(extra_hours or 0)
    return projected > row["capacity"], _q(projected - row["capacity"])


# ---------------------------------------------------------------------------
# grids (weekly / monthly planner + heatmap)
# ---------------------------------------------------------------------------

def planner_grid(window, *, today=None):
    """People × days, each cell carrying hours, capacity, utilisation, status.

    Three queries feed the whole grid — leave, logged hours and scheduled hours
    all come back pre-grouped by (user, day) — and the nested loop below is
    pure dictionary lookups.
    """
    today = today or timezone.localdate()
    profiles = list(selectors.plannable_users(
        department_id=window.department_id, user_id=window.user_id))
    users = [profile.user for profile in profiles]
    user_ids = [user.pk for user in users]
    days = window.days
    if not user_ids:
        return {"days": days, "rows": [], "tasks_by_cell": {}}

    capacities = selectors.capacity_profiles(users)
    calendar = cap.LeaveCalendar(
        selectors.leave_records(window.start, window.end, user_ids=user_ids))
    logged = selectors.logged_hours_by_user_day(
        window.start, window.end, user_ids=user_ids)
    scheduled = selectors.scheduled_hours_by_user_day(
        window.start, window.end, user_ids=user_ids)

    tasks_by_cell = {}
    for task in selectors.planner_tasks(window.start, window.end, user_ids=user_ids):
        tasks_by_cell.setdefault((task.assignee_id, task.due_date), []).append(task)

    rows = []
    for profile in profiles:
        user = profile.user
        capacity_profile = capacities[user.pk]
        cells, row_allocated, row_capacity = [], ZERO, ZERO

        for day in days:
            day_capacity = cap.daily_capacity(capacity_profile, day, calendar)
            day_logged = Decimal(logged.get((user.pk, day)) or 0)
            day_scheduled = Decimal(scheduled.get((user.pk, day)) or 0)
            day_allocated = day_logged + day_scheduled
            leave_entry = calendar.entry_for(user.pk, day)
            utilization = cap.utilization_percent(day_allocated, day_capacity)

            row_allocated += day_allocated
            row_capacity += day_capacity
            cells.append({
                "date": day,
                "is_today": day == today,
                "is_past": day < today,
                "is_working_day": capacity_profile.works_on(day),
                "capacity": _q(day_capacity),
                "logged": _q(day_logged),
                "scheduled": _q(day_scheduled),
                "allocated": _q(day_allocated),
                "utilization_percent": utilization,
                "status": cap.status_for(utilization, day_capacity),
                # 0-4, drives the heatmap shade. Kept coarse on purpose: a
                # continuous gradient reads as noise at a glance, and the
                # numeric hours are printed in the cell regardless.
                "heat": _heat_level(utilization, day_capacity),
                "leave": leave_entry,
                "pending_leave": calendar.has_pending(user.pk, day),
                "tasks": tasks_by_cell.get((user.pk, day), []),
            })

        row_utilization = cap.utilization_percent(row_allocated, row_capacity)
        rows.append({
            "user": user,
            "employee": profile,
            "department": user.department,
            "cells": cells,
            "capacity": _q(row_capacity),
            "allocated": _q(row_allocated),
            "remaining": _q(row_capacity - row_allocated),
            "utilization_percent": row_utilization,
            "status": cap.status_for(row_utilization, row_capacity),
            "status_label": cap.STATUS_LABELS[
                cap.status_for(row_utilization, row_capacity)],
        })

    rows.sort(key=lambda row: row["utilization_percent"], reverse=True)
    return {"days": days, "rows": rows,
            "totals": _column_totals(days, rows)}


def _heat_level(utilization, capacity):
    """Five buckets, aligned to the same thresholds as the colour bands so the
    heatmap and the status pills never tell different stories."""
    if capacity is not None and capacity <= 0:
        return 0
    if utilization <= 0:
        return 0
    if utilization < 50:
        return 1
    if utilization < cap.AVAILABLE_BELOW:
        return 2
    if utilization < cap.NEAR_CAPACITY_BELOW:
        return 3
    return 4


def _column_totals(days, rows):
    """Per-day team totals under the grid — the answer to "is Thursday the
    problem, or is it everybody all week"."""
    totals = []
    for index, day in enumerate(days):
        capacity_total = sum((row["cells"][index]["capacity"] for row in rows), ZERO)
        allocated_total = sum((row["cells"][index]["allocated"] for row in rows), ZERO)
        utilization = cap.utilization_percent(allocated_total, capacity_total)
        totals.append({
            "date": day,
            "capacity": _q(capacity_total),
            "allocated": _q(allocated_total),
            "utilization_percent": utilization,
            "status": cap.status_for(utilization, capacity_total),
        })
    return totals


# ---------------------------------------------------------------------------
# forecast
# ---------------------------------------------------------------------------

def capacity_forecast(weeks=8, *, department_id=None, today=None):
    """Week-by-week capacity against committed work, looking forward.

    Deliberately built from the same `workload()` call per week rather than a
    bespoke query: a forecast that used different arithmetic from the planner
    would be the first thing to disagree with it.
    """
    today = today or timezone.localdate()
    start = cap.week_start(today)
    forecast = []
    for index in range(weeks):
        week_start = start + timedelta(days=7 * index)
        window = Window(start=week_start, end=week_start + timedelta(days=6),
                        department_id=department_id, mode="week")
        summary = summarise(workload(window, today=today))
        forecast.append({
            "start": week_start,
            "end": week_start + timedelta(days=6),
            "is_current": week_start == start,
            **summary,
        })
    return forecast


# ---------------------------------------------------------------------------
# department + agency analytics
# ---------------------------------------------------------------------------

def department_workload(window, *, today=None):
    """Per-department rollup, computed from the person rows so the department
    total is always the sum of the people shown inside it."""
    rows = workload(Window(start=window.start, end=window.end,
                           project_id=window.project_id), today=today)
    buckets = {}
    for row in rows:
        department = row["department"]
        key = department.pk if department else None
        bucket = buckets.setdefault(key, {
            "department": department, "members": [],
            "capacity": ZERO, "allocated": ZERO, "leave_hours": ZERO,
        })
        bucket["members"].append(row)
        bucket["capacity"] += row["capacity"]
        bucket["allocated"] += row["allocated_hours"]
        bucket["leave_hours"] += row["leave_hours"]

    results = []
    for bucket in buckets.values():
        utilization = cap.utilization_percent(bucket["allocated"], bucket["capacity"])
        results.append({
            "department": bucket["department"],
            "name": bucket["department"].name if bucket["department"] else "Unassigned",
            "headcount": len(bucket["members"]),
            "members": bucket["members"],
            "capacity": _q(bucket["capacity"]),
            "allocated": _q(bucket["allocated"]),
            "remaining": _q(bucket["capacity"] - bucket["allocated"]),
            "leave_hours": _q(bucket["leave_hours"]),
            "utilization_percent": utilization,
            "status": cap.status_for(utilization, bucket["capacity"]),
            "status_label": cap.STATUS_LABELS[
                cap.status_for(utilization, bucket["capacity"])],
            "over_count": sum(1 for member in bucket["members"]
                              if member["status"] == cap.STATUS_OVER),
        })
    results.sort(key=lambda row: row["utilization_percent"], reverse=True)
    return results


def utilization_leaders(rows, *, limit=5):
    """Top and bottom of the utilisation table.

    People with no capacity in the window are excluded from both ends: someone
    on leave all week is not the agency's most underutilised resource.
    """
    ranked = [row for row in rows if row["capacity"] > 0]
    ranked.sort(key=lambda row: row["utilization_percent"], reverse=True)
    return {
        "top": ranked[:limit],
        "bottom": list(reversed(ranked[-limit:])) if ranked else [],
    }
