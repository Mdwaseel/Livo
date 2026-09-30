"""Queries against the calendar's *own* tables, plus the filter dropdowns.

The division of labour is different from the other modules here, and
deliberately so. In `analytics` and `resource_planner`, `selectors.py` is the
only file that touches the ORM. In a module whose job is to aggregate seven
other apps, forcing every query through one file would mean this module knowing
the internals of `Task`, `Milestone`, `Document` and the rest — exactly the
coupling the source registry exists to avoid.

So: each source owns its own query into its own app (`sources/`), and this file
owns queries into `CalendarEvent`, `EventOccurrence` and `ReminderLog`, plus the
shared dropdown options. Both halves obey the same house rules — one query per
question, `select_related` on anything a template iterates, nothing evaluated
inside a loop.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Q

from accounts.models import Department
from clients.access import visible_clients
from clients.models import Client
from core.tenancy import scope
from projects.access import visible_projects
from projects.models import Project

from .models import CalendarEvent, EventOccurrence, ReminderLog

User = get_user_model()


# ---------------------------------------------------------------------------
# the calendar's own events
# ---------------------------------------------------------------------------

def visible_events(user):
    """Every event this user is allowed to load.

    Applied in SQL rather than filtered after the fact: a private one-to-one
    about somebody's performance should never be read out of the database for a
    person who isn't in it, not even to be dropped a moment later.
    """
    queryset = scope(
        CalendarEvent.objects
        .select_related("project", "project__client", "client",
                        "department", "created_by")
        .prefetch_related("attendees"),
        user)
    pk = getattr(user, "pk", None)
    if pk is None:
        return queryset.filter(visibility=CalendarEvent.Visibility.AGENCY)
    return queryset.filter(
        Q(visibility=CalendarEvent.Visibility.AGENCY)
        | Q(created_by_id=pk) | Q(attendees__id=pk)).distinct()


def event_or_none(pk, user):
    return visible_events(user).filter(pk=pk).first()


def overrides_for(event):
    return EventOccurrence.objects.filter(event=event).order_by("original_date")


def events_needing_reminders(start, end):
    """Events whose reminder window could fall in [start, end].

    Recurring series are returned whole and expanded by the caller, which
    already owns the expansion logic — duplicating the rule walk in a query
    would be a second implementation of the same arithmetic.
    """
    # Deliberately unscoped: this is the nightly reminder job, which is system
    # context and reminds every workspace's attendees about their own events.
    return (CalendarEvent.objects
            .filter(is_cancelled=False, reminder_minutes__isnull=False)
            .filter(Q(start_date__lte=end)
                    & (Q(frequency=CalendarEvent.Frequency.NONE,
                         start_date__gte=start - timedelta(days=1))
                       | ~Q(frequency=CalendarEvent.Frequency.NONE)))
            .select_related("project", "created_by")
            .prefetch_related("attendees")
            .order_by("start_date", "start_time"))


def already_reminded(source_keys, kind):
    """{(source_key, event_date, user_id)} already delivered — one query.

    The dispatcher checks against this set in memory instead of asking the
    database per recipient, which is what turns a nightly run over a few hundred
    reminders into two queries rather than a few hundred.
    """
    if not source_keys:
        return set()
    return {
        (row["source_key"], row["event_date"], row["user_id"])
        for row in ReminderLog.objects
        .filter(source_key__in=list(source_keys), kind=kind)
        .values("source_key", "event_date", "user_id")
    }


# ---------------------------------------------------------------------------
# filter dropdowns
# ---------------------------------------------------------------------------

def filter_options(viewer=None):
    """Everything the filter bar needs, in four queries.

    `only()` throughout: these selects need an id and a label. Without it the
    employee list drags `salary` and the bank columns into a page that has no
    business holding them — the same reason `analytics.selectors.filter_options`
    does it.
    """
    return {
        "employees": (User.objects.filter(is_active=True)
                      .only("id", "first_name", "last_name", "username")
                      .order_by("first_name", "username")),
        "departments": Department.objects.only("id", "name").order_by("name"),
        "projects": (visible_projects(
                        Project.objects.filter(is_archived=False), viewer)
                     .select_related("client")
                     .only("id", "name", "client__name")
                     .order_by("client__name", "name")),
        "clients": (visible_clients(
                        Client.objects.filter(is_archived=False), viewer)
                    .only("id", "name").order_by("name")),
    }


def attendee_choices():
    """Who can be invited to a meeting."""
    return (User.objects.filter(is_active=True)
            .only("id", "first_name", "last_name", "username")
            .order_by("first_name", "username"))
