"""Recurrence expansion. Pure arithmetic — no ORM, no request, no I/O.

A recurring event is stored once, as a rule, and turned into concrete dates only
when a window asks for them. That is the same choice
`projects.RecurringTask` made for spawning tasks, and it holds here for a
stronger reason: a task that recurs produces real work somebody has to do, so it
earns a row; a stand-up that recurs produces nothing but a line on a grid, and
writing 260 rows a year for it would be storage in place of a calculation.

The rule supported is a deliberate subset of iCalendar's RRULE — FREQ, INTERVAL,
BYDAY, UNTIL, COUNT — chosen because it covers what an agency actually
schedules and maps one-for-one onto what Google, Outlook and Apple accept. The
parts left out (BYSETPOS, BYMONTHDAY lists, "last Friday of the month") are
absent rather than half-implemented, so `sync/ical.py` never has to emit a rule
it cannot faithfully round-trip.
"""
from __future__ import annotations

import calendar as stdlib_calendar
from datetime import date, timedelta

from .models import MAX_OCCURRENCES, CalendarEvent


def expand(event, start, end, *, overrides=None):
    """Every date `event` lands on between `start` and `end`, inclusive.

    Returns a list of ``(occurrence_date, effective_date, override)`` triples.
    The two dates differ only when an exception moved the instance:
    `occurrence_date` is the rule's answer and stays the instance's identity,
    `effective_date` is where it actually shows up. Cancelled instances are
    dropped entirely.

    `overrides` is ``{original_date: EventOccurrence}``, passed in rather than
    fetched so the caller can load every override for every event in one query
    instead of one per series.
    """
    overrides = overrides or {}
    # How long one occurrence lasts. A three-day offsite that recurs monthly is
    # three days each month, so the span travels with every instance.
    span = event.last_date - event.start_date

    if not event.is_recurring:
        # A non-recurring event still goes through here so callers have exactly
        # one code path.
        return _apply(event.start_date, overrides, start, end, span)

    results = []
    for occurrence in _rule_dates(event, end):
        # An instance can be *moved into* the window from before it, so the
        # override has to be resolved before the window is checked.
        results.extend(_apply(occurrence, overrides, start, end, span))
    return results


def _apply(occurrence_date, overrides, start, end, span):
    override = overrides.get(occurrence_date)
    if override is not None:
        if override.is_cancelled:
            return []
        effective = override.effective_date
    else:
        effective = occurrence_date
    # An overlap, not a containment. A fortnight of leave that began last month
    # is still happening today, and testing only the start date is how a
    # multi-day event silently disappears from every window but its first.
    if effective <= end and effective + span >= start:
        return [(occurrence_date, effective, override)]
    return []


def _rule_dates(event, horizon):
    """The raw dates the rule produces, up to `horizon`.

    Generated from the series start every time rather than from the window, so
    COUNT means what it says: "the 10th occurrence" is the 10th since the series
    began, not the 10th since whichever month happens to be on screen.
    """
    limit = min(event.repeat_count or MAX_OCCURRENCES, MAX_OCCURRENCES)
    until = event.repeat_until
    if until is not None:
        horizon = min(horizon, until)

    dates, produced = [], 0
    for candidate in _step(event):
        if candidate > horizon:
            break
        dates.append(candidate)
        produced += 1
        if produced >= limit:
            break
    return dates


def _step(event):
    """Generate the rule's dates in order, indefinitely.

    Bounded by its callers, never by itself: `_rule_dates` owns both the count
    limit and the horizon, so there is one place to look when asking why a
    series stopped.
    """
    frequency = event.frequency
    interval = max(1, event.interval or 1)
    anchor = event.start_date

    if frequency == CalendarEvent.Frequency.DAILY:
        cursor = anchor
        while True:
            yield cursor
            cursor += timedelta(days=interval)

    elif frequency == CalendarEvent.Frequency.WEEKLY:
        weekdays = event.weekday_numbers
        # Walk from the Monday of the start's week so a multi-weekday rule
        # ("Mon and Thu") produces both days of the first week, including the
        # one before the start date — which is then filtered out below.
        week = anchor - timedelta(days=anchor.isoweekday() - 1)
        while True:
            for weekday in weekdays:
                candidate = week + timedelta(days=weekday - 1)
                if candidate >= anchor:
                    yield candidate
            week += timedelta(days=7 * interval)

    elif frequency == CalendarEvent.Frequency.MONTHLY:
        # Clamped against the ANCHOR day, not against last month's clamped
        # result — the same trap `RecurringTask._step` documents. Stepping from
        # the clamped date makes a 31st series stick at the 28th forever after
        # its first February.
        index = 0
        while True:
            yield _add_months(anchor, index * interval)
            index += 1

    elif frequency == CalendarEvent.Frequency.YEARLY:
        index = 0
        while True:
            yield _add_years(anchor, index * interval)
            index += 1

    else:  # NONE — one date, then stop.
        yield anchor


def _add_months(anchor, months):
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    return date(year, month,
                min(anchor.day, stdlib_calendar.monthrange(year, month)[1]))


def _add_years(anchor, years):
    year = anchor.year + years
    # 29 February on a common year becomes the 28th rather than vanishing.
    return date(year, anchor.month,
                min(anchor.day, stdlib_calendar.monthrange(year, anchor.month)[1]))


def occurrence_key(event_pk, occurrence_date):
    """Stable identity for one instance of a series.

    The date is part of the key because the reminder ledger has to be able to
    say "already reminded about the 3 August stand-up" without that also
    silencing the 10 August one.
    """
    return f"event:{event_pk}@{occurrence_date.isoformat()}"
