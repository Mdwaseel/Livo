"""Collection and layout: sources in, grids out.

Two responsibilities, and nothing else. `collect` asks every source that can
answer the query and merges the results into one sorted list of `Event`s.
Everything below it arranges that list into whichever shape a view needs — a
month of weeks, a week of days, one day, or a paginated agenda.

No ORM here beyond what the sources do on their own, and no knowledge of any
model: this file could not name `Task` if it wanted to. That is what keeps
"add a source" from meaning "edit the calendar".
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta

from django.utils import timezone

from . import sources
from .events import KIND_ORDER, KINDS

# How many chips a month cell shows before collapsing into "+3 more". Six is
# roughly what fits at the narrowest desktop column without the row growing
# taller than its neighbours; the rest arrive when the day is opened.
MONTH_CELL_LIMIT = 4
# Agenda page size, in days rather than events — a page that cuts a Tuesday in
# half reads as missing data.
AGENDA_PAGE_DAYS = 14


# ---------------------------------------------------------------------------
# the window
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    """The period on screen, and how to step off it.

    Separate from `CalendarQuery` because the two answer different questions:
    the window is what the *user* asked to look at (August, this week, today),
    while the query is what the *sources* are asked for — which is wider, since
    a month grid spills into the last days of July and the first of September.
    """

    anchor: date
    mode: str  # month | week | day | agenda

    @property
    def start(self):
        """First day of the period itself, not of the grid around it."""
        if self.mode == "month":
            return self.anchor.replace(day=1)
        if self.mode == "week":
            return week_start(self.anchor)
        return self.anchor

    @property
    def end(self):
        if self.mode == "month":
            return month_end(self.anchor)
        if self.mode == "week":
            return week_start(self.anchor) + timedelta(days=6)
        if self.mode == "agenda":
            return self.anchor + timedelta(days=AGENDA_PAGE_DAYS - 1)
        return self.anchor

    @property
    def grid_start(self):
        """First day the *sources* are asked for.

        A month grid shows the trailing days of the previous month in its first
        row. Fetching only the month would leave those cells blank, which reads
        as "nothing happened" rather than "not asked".
        """
        return week_start(self.start) if self.mode == "month" else self.start

    @property
    def grid_end(self):
        if self.mode == "month":
            return week_start(self.end) + timedelta(days=6)
        return self.end

    @property
    def previous(self):
        if self.mode == "month":
            return (self.start - timedelta(days=1)).replace(day=1)
        if self.mode == "week":
            return self.start - timedelta(days=7)
        if self.mode == "agenda":
            return self.start - timedelta(days=AGENDA_PAGE_DAYS)
        return self.start - timedelta(days=1)

    @property
    def next(self):
        if self.mode == "month":
            return month_end(self.anchor) + timedelta(days=1)
        if self.mode == "week":
            return self.start + timedelta(days=7)
        if self.mode == "agenda":
            return self.start + timedelta(days=AGENDA_PAGE_DAYS)
        return self.start + timedelta(days=1)

    @property
    def title(self):
        if self.mode == "month":
            return f"{self.start:%B %Y}"
        if self.mode == "day":
            return f"{self.start:%A %d %B %Y}"
        if self.start.year != self.end.year:
            return f"{self.start:%d %b %Y} – {self.end:%d %b %Y}"
        if self.start.month != self.end.month:
            return f"{self.start:%d %b} – {self.end:%d %b %Y}"
        return f"{self.start:%d} – {self.end:%d %b %Y}"

    @property
    def days(self):
        return list(date_range(self.start, self.end))


def week_start(day):
    """Monday of `day`'s week — matching `resource_planner.capacity.week_start`,
    so a person switching between the planner and the calendar sees the same
    week boundaries."""
    return day - timedelta(days=day.weekday())


def month_end(day):
    import calendar as stdlib_calendar

    return day.replace(
        day=stdlib_calendar.monthrange(day.year, day.month)[1])


def date_range(start, end):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------

def collect(query):
    """Every event in the window, from every source that can answer.

    Sources are asked in registration order and their results merged, so the
    number of queries the calendar runs is the number of sources that survived
    `sources_for` — a fixed, small number that does not grow with how much data
    the window contains. `tests/test_performance.py` measures it.
    """
    events = []
    for source in sources.sources_for(query):
        # `is_movable` is resolved to *this viewer* here, once per source,
        # rather than left as a property of the record and re-checked per chip.
        # A month grid can carry two hundred chips; asking the permission engine
        # two hundred times to render them would be the N+1 of the template
        # layer.
        allowed = source.can_move(query.viewer)
        for event in source.fetch(query):
            events.append(event if allowed or not event.is_movable
                          else replace(event, is_movable=False))
    events.sort(key=lambda event: (event.start_date, event.sort_key))
    return events


def counts_by_kind(events):
    """{kind_key: count} in legend order, for the filter chips.

    Kinds with nothing in the window are included with a zero so the legend
    doesn't reshuffle every time somebody changes month.
    """
    counts = {key: 0 for key in KIND_ORDER}
    for event in events:
        counts[event.kind] = counts.get(event.kind, 0) + 1
    return [
        {"kind": KINDS[key], "count": counts.get(key, 0)}
        for key in KIND_ORDER
    ]


def bucket_by_day(events, start, end):
    """{date: [events]} across the whole range, empty days included.

    Multi-day events land in every day they cover, clipped to the range — a
    fortnight of leave has to be visible on the Wednesday you're looking at, not
    only on the Monday it began.
    """
    buckets = {day: [] for day in date_range(start, end)}
    for event in events:
        for day in event.dates_within(start, end):
            if day in buckets:
                buckets[day].append(event.at(day) if event.is_multi_day else event)
    for day_events in buckets.values():
        day_events.sort(key=lambda event: event.sort_key)
    return buckets


# ---------------------------------------------------------------------------
# the four views
# ---------------------------------------------------------------------------

def month_grid(window, query, *, today=None):
    """Weeks of days, each carrying its (capped) events.

    The cap is a display decision, not a data one: every event is still counted
    in `total`, and `hidden` says how many the cell is holding back, so a busy
    day never silently looks like a quiet one.
    """
    today = today or timezone.localdate()
    events = collect(query)
    buckets = bucket_by_day(events, window.grid_start, window.grid_end)
    month = window.start.month

    weeks, current = [], []
    for day in date_range(window.grid_start, window.grid_end):
        day_events = buckets[day]
        current.append({
            "date": day,
            "events": day_events[:MONTH_CELL_LIMIT],
            "hidden": max(0, len(day_events) - MONTH_CELL_LIMIT),
            "total": len(day_events),
            "is_today": day == today,
            "is_other_month": day.month != month,
            "is_weekend": day.isoweekday() >= 6,
            "is_past": day < today,
        })
        if len(current) == 7:
            weeks.append(current)
            current = []
    if current:
        weeks.append(current)
    return {"weeks": weeks, "events": events}


def week_grid(window, query, *, today=None):
    """Seven day columns, with all-day entries split out from timed ones.

    The split is the whole point of a week view: an all-day marker stretched
    down a time axis is a lie about when it happens, and a 30-minute stand-up
    drawn the same height as a week of leave is unreadable.
    """
    today = today or timezone.localdate()
    events = collect(query)
    buckets = bucket_by_day(events, window.start, window.end)

    columns = []
    for day in window.days:
        day_events = buckets[day]
        columns.append({
            "date": day,
            "all_day": [event for event in day_events if event.is_all_day],
            "timed": [event for event in day_events if not event.is_all_day],
            "total": len(day_events),
            "is_today": day == today,
            "is_weekend": day.isoweekday() >= 6,
            "is_past": day < today,
        })
    return {"columns": columns, "events": events}


def day_detail(window, query, *, today=None):
    """One day, everything on it, nothing collapsed."""
    today = today or timezone.localdate()
    events = collect(query)
    day = window.start
    on_this_day = [event.at(day) if event.is_multi_day else event
                   for event in events if event.covers(day)]
    on_this_day.sort(key=lambda event: event.sort_key)
    return {
        "date": day,
        "all_day": [event for event in on_this_day if event.is_all_day],
        "timed": [event for event in on_this_day if not event.is_all_day],
        "events": on_this_day,
        "is_today": day == today,
    }


def agenda(window, query, *, today=None):
    """A flat, chronological list — days with nothing on them omitted.

    The opposite decision from the month grid, and for the opposite reason: a
    grid is a map, so blank cells are meaningful, while an agenda is a list, and
    fourteen "nothing on Tuesday" rows are just scrolling.
    """
    today = today or timezone.localdate()
    events = collect(query)
    buckets = bucket_by_day(events, window.start, window.end)
    days = [
        {"date": day, "events": day_events, "is_today": day == today,
         "is_past": day < today}
        for day, day_events in sorted(buckets.items()) if day_events
    ]
    return {"days": days, "events": events,
            "total": sum(len(day["events"]) for day in days)}


# ---------------------------------------------------------------------------
# helpers the views share
# ---------------------------------------------------------------------------

def movable_sources(user):
    """{source_key: bool} — may this viewer drag this kind of thing?

    Resolved once per request and handed to the template, so a grid with two
    hundred chips on it asks the permission engine seven times rather than two
    hundred.
    """
    return {source.key: source.can_move(user) for source in sources.all_sources()}


def visible_sources(user):
    """The sources this viewer can see at all — drives the legend, so a person
    without `leaves.view` isn't shown a colour key for a colour they will never
    encounter."""
    return [source for source in sources.all_sources() if source.can_view(user)]


def visible_kinds(user):
    """Legend rows and category-filter options for this viewer."""
    allowed = {kind for source in visible_sources(user) for kind in source.kinds}
    return [KINDS[key] for key in KIND_ORDER if key in allowed]


def next_up(events, *, today=None, limit=5):
    """The soonest few things from an already-collected list.

    Takes the list rather than re-querying, so the "coming up" strip can never
    disagree with the grid printed underneath it — the same rule
    `resource_planner.services.summarise` follows.
    """
    today = today or timezone.localdate()
    ahead = [event for event in events if event.end_date >= today]
    ahead.sort(key=lambda event: (event.start_date, event.sort_key))
    return ahead[:limit]


def overdue(events, *, limit=8):
    """Everything in the window already past its date and not finished."""
    late = [event for event in events if event.is_overdue]
    late.sort(key=lambda event: event.start_date)
    return late[:limit]
