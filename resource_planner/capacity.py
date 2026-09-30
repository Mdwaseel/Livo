"""Capacity arithmetic: how many hours a person actually has, day by day.

Separated from `selectors.py` because none of it touches the database — it
takes a profile, a leave list and a date range and returns numbers. That makes
the rules testable in isolation, which matters here more than anywhere else in
the module: every utilisation percentage, every colour, and every overbooking
warning is this file's output divided into somebody's workload.

The rules, stated once:

1. A day counts only if it is one of the person's working days.
2. Approved leave removes that day. Half-day leave removes half of it.
3. Company-wide holidays (leave rows with no user) apply to everybody.
4. Requested-but-unapproved leave changes nothing — it is surfaced as a warning
   instead. Otherwise anyone could free up their calendar by asking.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

ZERO = Decimal("0")


def date_range(start, end):
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def week_start(day):
    """Monday of the week containing `day`. The planner is Monday-first because
    the default working week is."""
    return day - timedelta(days=day.weekday())


def month_start(day):
    return day.replace(day=1)


def month_end(day):
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def iter_weeks(start, end):
    """[(week_monday, week_sunday)] covering the range, clipped to it."""
    weeks, cursor = [], week_start(start)
    while cursor <= end:
        weeks.append((max(cursor, start), min(cursor + timedelta(days=6), end)))
        cursor += timedelta(days=7)
    return weeks


@dataclass
class LeaveDay:
    """One person's leave on one date, already reduced to its effect."""
    fraction: Decimal   # 1 = whole day off, 0.5 = half day
    kind: str
    is_company_wide: bool


class LeaveCalendar:
    """Leave, indexed by (user_id, date) so lookups are O(1) in the hot loop.

    Built once per request from a single query. The alternative — asking "is
    this person on leave today" per person per day — is 20 people × 31 days of
    queries for one monthly planner.
    """

    def __init__(self, records):
        self._by_user = {}
        self._company = {}
        self._pending = {}
        for record in records:
            target = (self._company if record.is_company_wide
                      else self._by_user.setdefault(record.user_id, {}))
            if not record.blocks_capacity:
                if record.status == record.Status.REQUESTED:
                    for day in date_range(record.start_date, record.end_date):
                        self._pending.setdefault(record.user_id, set()).add(day)
                continue
            fraction = Decimal("0.5") if record.is_half_day else Decimal("1")
            for day in date_range(record.start_date, record.end_date):
                existing = target.get(day)
                # Overlapping leave doesn't stack past a full day off — two
                # half-days on the same date is still one day, not one and a
                # half, which would make capacity negative.
                if existing is None or fraction > existing.fraction:
                    target[day] = LeaveDay(fraction, record.kind,
                                           record.is_company_wide)

    def fraction_off(self, user_id, day):
        """0, 0.5 or 1 — how much of `day` this person is away for."""
        company = self._company.get(day)
        personal = self._by_user.get(user_id, {}).get(day)
        fractions = [entry.fraction for entry in (company, personal) if entry]
        return max(fractions) if fractions else ZERO

    def entry_for(self, user_id, day):
        return (self._by_user.get(user_id, {}).get(day)
                or self._company.get(day))

    def has_pending(self, user_id, day):
        return day in self._pending.get(user_id, set())

    def days_off(self, user_id, start, end):
        return sum(self.fraction_off(user_id, day)
                   for day in date_range(start, end))


class EmptyLeaveCalendar(LeaveCalendar):
    """For callers that don't care about leave (tests, raw capacity)."""

    def __init__(self):
        super().__init__([])


def daily_capacity(profile, day, calendar=None):
    """Hours this person has available on this specific date."""
    if not profile.works_on(day):
        return ZERO
    hours = Decimal(profile.daily_hours)
    if calendar is None:
        return hours
    off = calendar.fraction_off(profile.user_id, day)
    if off >= 1:
        return ZERO
    return (hours * (Decimal("1") - off)).quantize(Decimal("0.01"))


def capacity_between(profile, start, end, calendar=None):
    """Total available hours across a date range, leave already deducted."""
    return sum((daily_capacity(profile, day, calendar)
                for day in date_range(start, end)), ZERO)


def gross_capacity_between(profile, start, end):
    """Capacity ignoring leave — the denominator for "how much of their
    contracted time is this person actually available for"."""
    return Decimal(profile.daily_hours) * profile.working_days_between(start, end)


def leave_hours_between(profile, start, end, calendar):
    """Hours lost to leave in the range. Reported alongside capacity so a low
    availability figure comes with its explanation."""
    return gross_capacity_between(profile, start, end) - capacity_between(
        profile, start, end, calendar)


# ---------------------------------------------------------------------------
# indicators
# ---------------------------------------------------------------------------

# Utilisation bands. Named because the legend, the CSS class and the analytics
# grouping all have to agree, and three copies of "75" would not stay agreed.
AVAILABLE_BELOW = 75      # green:  room for more work
NEAR_CAPACITY_BELOW = 100  # yellow: full, but not over
# anything at or above NEAR_CAPACITY_BELOW is red

STATUS_AVAILABLE = "available"
STATUS_NEAR = "near"
STATUS_OVER = "over"
STATUS_OFF = "off"

STATUS_LABELS = {
    STATUS_AVAILABLE: "Available",
    STATUS_NEAR: "Near capacity",
    STATUS_OVER: "Overloaded",
    STATUS_OFF: "No capacity",
}


def status_for(utilization_percent, capacity):
    """Green / yellow / red, plus a fourth state the brief didn't ask for.

    A person with zero capacity — a non-working day, or someone on leave all
    week — is not "available", and colouring them green would send a manager to
    hand work to somebody who is on a beach. They get their own neutral state.
    """
    if capacity is not None and capacity <= 0:
        return STATUS_OFF
    if utilization_percent >= NEAR_CAPACITY_BELOW:
        return STATUS_OVER
    if utilization_percent >= AVAILABLE_BELOW:
        return STATUS_NEAR
    return STATUS_AVAILABLE


def utilization_percent(allocated, capacity):
    """Allocated as a percentage of capacity, one decimal.

    Zero capacity with work allocated is infinite utilisation, which no bar can
    draw; it is reported as 100% over and `status_for` flags it red through the
    capacity argument anyway.
    """
    allocated = Decimal(allocated or 0)
    capacity = Decimal(capacity or 0)
    if capacity <= 0:
        return 200.0 if allocated > 0 else 0.0
    return float((allocated * 100 / capacity).quantize(Decimal("0.1")))
