"""The month, shaped for the screen. Shared by the planner and the public page.

Both pages ask the same question — "what is on this client's calendar this
month, narrowed how?" — so they get the same answer from one function, and the
client can never see a grid that differs from the one the team planned on.
The only difference between them is the queryset passed in: the public page
hands over `show_to_client=True` rows, the planner hands over everything the
viewer may see.

One query for the grid, one for "coming up", one for "last updated". Nothing
here grows with the number of activities.
"""
from dataclasses import dataclass
from datetime import date, time, timedelta
from urllib.parse import urlencode

from django.db.models import Max
from django.utils import timezone

from calendar_hub.services import Window, date_range, month_end  # noqa: F401

from .models import FAMILIES, ClientActivity

# Pills per month cell before "+n". Three reads at a glance on a laptop; the
# day sheet always shows everything.
CELL_LIMIT = 3
UPCOMING_DAYS = 7
VIEWS = ("grid", "list")

Status = ClientActivity.Status


def parse_anchor(raw, today):
    """`2026-09-14`, `2026-09`, or anything else -> today.

    The month form exists because it is what the stepper links use; a full
    date is what "add on this day" and deep links use. Years are clamped so a
    hand-typed `?date=0001-01` renders today rather than a traceback.
    """
    raw = (raw or "").strip()
    try:
        value = date.fromisoformat(raw + "-01" if len(raw) == 7 else raw)
    except ValueError:
        return today
    return value if 2000 <= value.year <= 2100 else today


def _positive_int(raw):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class Filters:
    anchor: date
    project_id: int | None
    family: str
    status: str
    view: str

    def link(self, **changes):
        """This page's querystring with some filters changed.

        Built here rather than in the template so every link on the page —
        stepper, chips, the approval tile — agrees on the grammar, and a filter
        survives moving between months.
        """
        values = {
            "date": f"{self.anchor:%Y-%m}",
            "project": self.project_id or "",
            "type": self.family,
            "status": self.status,
            "view": self.view,
        }
        values.update(changes)
        pairs = [(key, value) for key, value in values.items() if value not in ("", None)]
        return "?" + urlencode(pairs) if pairs else "?"


def parse_filters(params, today, project_ids):
    project_id = _positive_int(params.get("project"))
    family = params.get("type", "")
    status = params.get("status", "")
    view = params.get("view", "")
    return Filters(
        anchor=parse_anchor(params.get("date"), today),
        # An id that isn't one of this client's projects is dropped rather than
        # obeyed: on the public page it is somebody editing the URL.
        project_id=project_id if project_id in project_ids else None,
        family=family if family in FAMILIES else "",
        status=status if status in Status.values else "",
        view=view if view in VIEWS else "",
    )


def _sort_key(activity):
    # Timed entries in clock order first, then the ones with no time of day.
    return (activity.time is None, activity.time or time.min, activity.pk)


def summarise(activities):
    """The four numbers at the top of the page, for the month."""
    total = len(activities)
    done = sum(1 for a in activities if a.status == Status.DONE)
    postponed = sum(1 for a in activities if a.status == Status.POSTPONED)
    counted = total - postponed
    return {
        "total": total,
        "done": done,
        "approval": sum(1 for a in activities if a.status == Status.APPROVAL),
        "approved": sum(1 for a in activities if a.status == Status.APPROVED),
        "in_progress": sum(1 for a in activities if a.status == Status.IN_PROGRESS),
        "planned": sum(1 for a in activities if a.status == Status.PLANNED),
        "postponed": postponed,
        # Postponed work is neither delivered nor owed this month, so it leaves
        # the denominator rather than dragging the progress bar down.
        "percent": round(done * 100 / counted) if counted else 0,
    }


def _day_label(day):
    return f"{day:%A}, {day.day} {day:%B}"


def _cell_label(day, items):
    """What a screen reader hears for a month cell."""
    if not items:
        return f"{_day_label(day)}: nothing scheduled"
    parts = [f"{len(items)} activit{'y' if len(items) == 1 else 'ies'}"]
    approvals = sum(1 for a in items if a.needs_approval)
    if approvals:
        parts.append(f"{approvals} need{'s' if approvals == 1 else ''} your approval")
    return f"{_day_label(day)}: " + ", ".join(parts)


def build(base_qs, params, *, projects, today=None):
    today = today or timezone.localdate()
    projects = list(projects)
    filters = parse_filters(params, today, {p.pk for p in projects})
    window = Window(anchor=filters.anchor, mode="month")

    scoped = base_qs.select_related("project", "project__client")
    if filters.project_id:
        scoped = scoped.filter(project_id=filters.project_id)

    in_grid = sorted(
        scoped.filter(date__gte=window.grid_start, date__lte=window.grid_end),
        key=lambda a: (a.date, *_sort_key(a)))
    in_month = [a for a in in_grid if window.start <= a.date <= window.end]

    # The chips count the month before the type filter is applied, so choosing
    # "Ad campaigns" never makes the other chips read zero.
    family_counts = {key: 0 for key in FAMILIES}
    for activity in in_month:
        family_counts[activity.family] += 1

    shown = [a for a in in_grid
             if (not filters.family or a.family == filters.family)
             and (not filters.status or a.status == filters.status)]
    by_day = {}
    for activity in shown:
        by_day.setdefault(activity.date, []).append(activity)

    weeks, row = [], []
    for day in date_range(window.grid_start, window.grid_end):
        items = by_day.get(day, [])
        row.append({
            "date": day,
            "iso": day.isoformat(),
            "items": items,
            "shown": items[:CELL_LIMIT],
            "more": max(0, len(items) - CELL_LIMIT),
            "count": len(items),
            "approvals": sum(1 for a in items if a.needs_approval),
            "is_today": day == today,
            "is_other": day.month != window.start.month,
            "is_past": day < today,
            "is_weekend": day.isoweekday() >= 6,
            "label": _cell_label(day, items),
        })
        if len(row) == 7:
            weeks.append(row)
            row = []

    # Every grid day with something on it, in order. The list view shows the
    # month's; the day sheet can open any of them, including the neighbouring
    # month's days visible at the edges of the grid.
    days = [{
        "date": day,
        "iso": day.isoformat(),
        "label": _day_label(day),
        "items": by_day[day],
        "is_today": day == today,
        "is_other": day.month != window.start.month,
    } for day in date_range(window.grid_start, window.grid_end) if day in by_day]

    # The phone grid's "next up" list: the next three busy days from today in
    # this month, or the first three of a month still to come. A month already
    # over gets none — "next up" in August, read in October, is nonsense.
    month_days = [d for d in days if not d["is_other"]]
    if window.end < today:
        agenda = []
    elif window.start <= today:
        agenda = [d for d in month_days if d["date"] >= today][:3]
    else:
        agenda = month_days[:3]

    upcoming = (scoped.filter(date__gte=today,
                              date__lt=today + timedelta(days=UPCOMING_DAYS))
                .exclude(status__in=[Status.DONE, Status.POSTPONED]).count())
    last_updated = base_qs.aggregate(latest=Max("updated_at"))["latest"]

    return {
        "window": window,
        "filters": filters,
        "today": today,
        "weeks": weeks,
        "days": days,
        "month_days": month_days,
        "agenda": agenda,
        "summary": summarise(in_month),
        "upcoming": upcoming,
        "last_updated": last_updated,
        "families": [{
            "key": key,
            "label": label,
            "count": family_counts[key],
            "selected": filters.family == key,
            "url": filters.link(type="" if filters.family == key else key),
        } for key, (label, _) in FAMILIES.items()],
        "all_types_url": filters.link(type=""),
        "projects": [{
            "pk": project.pk,
            "name": project.name,
            "selected": filters.project_id == project.pk,
            "url": filters.link(project=project.pk),
        } for project in projects],
        "all_projects_url": filters.link(project=""),
        "selected_project": next((p for p in projects if p.pk == filters.project_id), None),
        "prev_url": filters.link(date=f"{window.previous:%Y-%m}"),
        "next_url": filters.link(date=f"{window.next:%Y-%m}"),
        "today_url": filters.link(date=""),
        "grid_url": filters.link(view="grid"),
        "list_url": filters.link(view="list"),
        "approval_url": filters.link(status=Status.APPROVAL, view="list"),
        "clear_status_url": filters.link(status=""),
        "status_filter_label": dict(Status.choices).get(filters.status, ""),
        "is_filtered": bool(filters.family or filters.status or filters.project_id),
        "clear_url": filters.link(type="", status="", project=""),
        "is_current_month": window.start <= today <= window.end,
    }
