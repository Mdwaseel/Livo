"""The wire format every source speaks.

This module holds no queries and no business rules. It defines one immutable
value object — `Event` — plus the catalog of event kinds, and that is
deliberately all it does: `Event` is the contract between the seven source
adapters, the four calendar views, the reminder engine and the iCalendar
serialiser. Everything downstream of a source works in `Event`s and never
touches a `Task`, a `LeaveRecord` or a `Document` again.

That single indirection is what makes the module aggregate rather than
duplicate. A task due date is not copied into a calendar table; it is *read* out
of `projects.Task` and dressed as an `Event` on the way past. Nothing is stored,
so nothing can go stale, and deleting the task removes it from the calendar with
no cleanup anywhere.

It is also the seam the external providers plug into: a Google or Outlook
integration is a thing that produces and consumes `Event`s. See `sync/base.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta


# ---------------------------------------------------------------------------
# kinds
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EventKind:
    """One row of the colour-coded legend.

    `marker` is the redundant, non-colour signal. Colour alone fails for a
    colour-blind reader and disappears entirely in a printed month view, so
    every chip carries its marker glyph and its kind label as text as well —
    the same rule the analytics charts already follow.
    """

    key: str
    label: str
    plural: str
    marker: str
    # CSS class suffix; the palette lives in app.css next to the design tokens
    # rather than being hardcoded here, so a rebrand is one stylesheet edit.
    tone: str
    description: str = ""


KINDS = {
    kind.key: kind
    for kind in (
        EventKind("task", "Task due", "Tasks due", "◆", "task",
                  "A task's due date."),
        EventKind("milestone", "Milestone", "Milestones", "◇", "milestone",
                  "A project checkpoint."),
        EventKind("project", "Project deadline", "Project deadlines", "▲",
                  "project", "A project's target end date."),
        EventKind("meeting", "Meeting", "Meetings", "●", "meeting",
                  "A scheduled meeting."),
        EventKind("event", "Event", "Events", "○", "event",
                  "A scheduled event, one-off or recurring."),
        EventKind("client_activity", "Client content", "Client content", "◈",
                  "client", "A post, story, ad change or report on a client's "
                  "shared calendar."),
        EventKind("leave", "Leave", "Leave", "▬", "leave",
                  "Somebody is away."),
        EventKind("holiday", "Holiday", "Holidays", "★", "holiday",
                  "A company-wide non-working day."),
        EventKind("birthday", "Birthday", "Birthdays", "✦", "birthday",
                  "An employee's birthday."),
        EventKind("document_review", "Document review", "Document reviews", "▣",
                  "document", "A document's review deadline."),
    )
}

# Order the legend and the category filter are rendered in — roughly "work I owe"
# first, "things about people" last.
KIND_ORDER = ["task", "milestone", "project", "meeting", "event",
              "client_activity", "document_review", "leave", "holiday", "birthday"]


def kind_choices():
    """(key, label) pairs for the category filter, in legend order."""
    return [(key, KINDS[key].plural) for key in KIND_ORDER]


# ---------------------------------------------------------------------------
# the event
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    """One thing that happens on one or more days.

    Frozen because events are derived, not owned: an `Event` is a snapshot of a
    row somebody else is responsible for, and letting a view mutate one would
    create the illusion of an edit that is never written anywhere. Changes go
    through the owning app — see `sources.base.EventSource.move`.

    Identity is `key`, a string of the form ``"<source>:<pk>"``, with a
    ``@<iso-date>`` suffix for one occurrence of a recurring series. It has to be
    a string rather than a pk because the calendar's rows come from six
    different tables plus expansions that have no row at all, and the reminder
    ledger has to be able to name any of them.
    """

    key: str
    kind: str
    title: str
    start_date: date
    # Inclusive. Equal to `start_date` for a single-day event; leave and project
    # spans are the multi-day cases.
    end_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None

    url: str = ""
    detail: str = ""
    # A short status chip: "Overdue", "Awaiting review", "Requested". Text, not
    # a colour, so it survives a monochrome print.
    status_label: str = ""
    is_overdue: bool = False

    # --- scoping: what the five filters match on ---
    user_id: int | None = None
    user_name: str = ""
    department_id: int | None = None
    project_id: int | None = None
    project_name: str = ""
    client_id: int | None = None
    client_name: str = ""

    # --- provenance ---
    source: str = ""
    object_id: int | None = None
    # Set on expanded occurrences of a recurring series; None otherwise. The
    # series' own `start_date` stays on the parent, so "which instance is this"
    # is never guessed from the date alone.
    occurrence_date: date | None = None
    # Filled by an external provider on the way in, and echoed back on the way
    # out, so a synced event round-trips instead of duplicating itself.
    external_id: str = ""

    # --- interaction ---
    # Whether *this viewer* may drag this to another day. A source sets it True
    # for records that have a date it knows how to change; `services.collect`
    # then clears it for viewers who lack the owning module's edit permission,
    # so a template can trust it without asking the permission engine per chip.
    is_movable: bool = False

    def __post_init__(self):
        if self.end_date is None:
            object.__setattr__(self, "end_date", self.start_date)

    # --- derived ---

    @property
    def is_all_day(self):
        return self.start_time is None

    @property
    def is_multi_day(self):
        return self.end_date > self.start_date

    @property
    def days(self):
        return (self.end_date - self.start_date).days + 1

    @property
    def kind_info(self):
        return KINDS[self.kind]

    @property
    def sort_key(self):
        """Timed events first and in clock order, then all-day ones by kind.

        A day column that leads with "Leave" and buries the 9am stand-up
        underneath it is answering a question nobody asked.
        """
        return (
            self.start_time is None,
            self.start_time or time.min,
            KIND_ORDER.index(self.kind) if self.kind in KIND_ORDER else 99,
            self.title.lower(),
        )

    @property
    def start_datetime(self):
        """A datetime for the reminder engine and the ICS export.

        An all-day event is anchored to 09:00 rather than midnight: a reminder
        for "tomorrow's deadline" fired at 00:00 lands in the middle of the
        night, and "1 day before" would mean 47 hours of notice or none at all
        depending on which side of midnight you read it from.
        """
        return datetime.combine(self.start_date, self.start_time or ALL_DAY_ANCHOR)

    def covers(self, day):
        return self.start_date <= day <= self.end_date

    def dates_within(self, start, end):
        """The days of this event that fall inside a window.

        Clipped to the window so a fortnight of leave doesn't render fourteen
        chips into a week view that only has room for five of them.
        """
        first = max(self.start_date, start)
        last = min(self.end_date, end)
        day = first
        while day <= last:
            yield day
            day += timedelta(days=1)

    def at(self, day, *, key_suffix=True):
        """This event as it appears on one specific day of a multi-day span."""
        if day == self.start_date and day == self.end_date:
            return self
        return replace(
            self,
            key=f"{self.key}@{day.isoformat()}" if key_suffix else self.key,
            occurrence_date=day,
        )


# 09:00 local. See `Event.start_datetime`.
ALL_DAY_ANCHOR = time(9, 0)


# ---------------------------------------------------------------------------
# the question
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CalendarQuery:
    """What is being asked of every source: a window, five filters, a search.

    The other half of the contract. A source takes one of these and returns
    `Event`s; nothing else passes between the calendar and the apps it reads.
    Holding it as a value object rather than as loose keyword arguments means
    adding a sixth filter is one field here plus one line per source that
    understands it, not a signature change in seven adapters and four views.

    `viewer` is on it because visibility is part of the question, not a
    post-filter: a source that can't be seen by this user must never run its
    query at all, so that private events and salary-adjacent records are never
    loaded and then discarded.
    """

    start: date
    end: date
    viewer: object = None
    employee_id: int | None = None
    department_id: int | None = None
    project_id: int | None = None
    client_id: int | None = None
    # Empty means every kind. Held as a frozenset so the query stays hashable.
    kinds: frozenset = frozenset()
    search: str = ""

    @property
    def days(self):
        return (self.end - self.start).days + 1

    @property
    def is_filtered(self):
        return any((self.employee_id, self.department_id, self.project_id,
                    self.client_id, self.kinds, self.search))

    @property
    def active_filters(self):
        """The scope filters that are actually set — what a source checks itself
        against to decide whether it can answer at all."""
        return {name for name, value in (
            ("employee", self.employee_id), ("department", self.department_id),
            ("project", self.project_id), ("client", self.client_id),
        ) if value}

    def wants(self, kind):
        return not self.kinds or kind in self.kinds

    def replace(self, **changes):
        return replace(self, **changes)
