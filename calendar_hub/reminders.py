"""Reminder and deadline-alert dispatch.

Delivered through `core.models.notify`, the in-app notification the topbar bell
already reads. Nothing new is invented for delivery; adding email later is one
call in `_deliver`, not a second notification system.

Two things are sent, and they are different in kind:

* **Reminders** — "your 3pm starts in an hour". Driven by
  `CalendarEvent.reminder_minutes`, sent to the attendees and the organiser,
  and expanded across recurrence so a daily stand-up reminds daily.
* **Deadline alerts** — "this is due tomorrow". Driven by the calendar's
  sources, sent to the person named on the record.

Correctness rests on two guards:

`ReminderLog` makes every send idempotent. The command can run every ten
minutes, twice by accident, or again after a crash, and each person hears about
each thing once. Without it, "every ten minutes" would mean exactly that.

`MAX_LATENESS` stops a backlog from detonating. If the scheduler was down for a
week, the run that comes back up must not fire four hundred reminders for
meetings that already happened — anything whose moment passed more than six
hours ago is dropped, silently and on purpose.

**Deliberate limitation.** Document review deadlines appear on the calendar but
send no automatic alert. Financial documents are visible only to people who hold
`finance.view` or the type's own module, and a background job has no viewer to
check that against — inventing one would be precisely the bypass that gate
exists to prevent. A source opts in by setting `sends_deadline_alerts`, and the
document source does not.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import Notification

from . import recurrence, selectors, sources
from .events import CalendarQuery
from .models import ReminderLog

# How far ahead a deadline alert looks. One day: "due tomorrow" is actionable,
# "due in a fortnight" is noise that trains people to ignore the bell.
DEADLINE_LEAD_DAYS = 1
# A reminder whose moment passed longer ago than this is dropped rather than
# sent late. See the module docstring.
MAX_LATENESS = timedelta(hours=6)
# How far forward to expand recurring series when looking for reminders. Two
# days covers any reminder lead time anyone sets in practice while keeping the
# expansion trivially small.
REMINDER_HORIZON_DAYS = 2


def run(*, now=None, dry_run=False):
    """Send everything that is due. Returns a summary dict.

    The single entry point — the management command is a thin wrapper, so the
    whole behaviour is testable without a scheduler.
    """
    now = now or timezone.localtime()
    reminders = meeting_reminders(now=now)
    alerts = deadline_alerts(today=now.date())
    sent = 0
    if not dry_run:
        sent += _deliver(reminders, ReminderLog.Kind.REMINDER)
        sent += _deliver(alerts, ReminderLog.Kind.DEADLINE)
    return {
        "reminders": len(reminders),
        "alerts": len(alerts),
        "sent": sent,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# what is due
# ---------------------------------------------------------------------------

def meeting_reminders(*, now=None):
    """[(source_key, event_date, user, text, url)] for meetings starting soon.

    Recurrence is expanded here rather than queried, because the rule walk
    already exists in `recurrence.py` and a second implementation in SQL would
    be a second thing to keep in step.
    """
    now = now or timezone.localtime()
    today = now.date()
    horizon = today + timedelta(days=REMINDER_HORIZON_DAYS)

    events = list(selectors.events_needing_reminders(today, horizon))
    overrides = _overrides_for(events)

    due = []
    for event in events:
        recipients = _recipients(event)
        if not recipients:
            continue
        for occurrence_date, effective, override in recurrence.expand(
                event, today, horizon, overrides=overrides.get(event.pk, {})):
            start = _start_datetime(event, effective, override, now)
            moment = start - timedelta(minutes=event.reminder_minutes)
            if not (now - MAX_LATENESS <= moment <= now):
                continue
            if start < now:
                # The meeting is already under way. Reminding someone about it
                # now is worse than saying nothing.
                continue
            key = (recurrence.occurrence_key(event.pk, occurrence_date)
                   if event.is_recurring else f"event:{event.pk}")
            when = (f"at {start:%H:%M}" if not event.is_all_day
                    else f"on {effective:%d %b}")
            for user in recipients:
                due.append((key, effective, user,
                            f"“{event.title}” starts {when}",
                            event.get_absolute_url()))
    return due


def deadline_alerts(*, today=None):
    """[(source_key, event_date, user, text, url)] for things due tomorrow.

    Built from the same source adapters the calendar draws from, so an alert can
    never describe a date the grid disagrees with.

    Visibility is not re-checked per recipient, and does not need to be: every
    alert goes to the person named on the record itself. There is no path here
    by which somebody learns about work that isn't theirs — which is also why
    sources whose entries have no owner, or whose visibility depends on the
    viewer, opt out rather than guessing a recipient.
    """
    today = today or timezone.localdate()
    target = today + timedelta(days=DEADLINE_LEAD_DAYS)
    query = CalendarQuery(start=target, end=target, viewer=None)

    due = []
    for source in sources.all_sources():
        if not source.sends_deadline_alerts:
            continue
        for event in source.fetch(query):
            user = _user_for(event)
            if user is None:
                continue
            due.append((
                event.key, event.start_date, user,
                f"{event.kind_info.label}: “{event.title}” is due tomorrow",
                event.url,
            ))
    return due


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------

def _deliver(items, kind):
    """Write the notifications and the ledger rows that stop a second send.

    Both in one transaction per item. A notification without its ledger row
    would be re-sent on the next run; a ledger row without its notification
    would silence a reminder that never arrived. Neither half is acceptable on
    its own, so neither is committed on its own.
    """
    if not items:
        return 0
    seen = selectors.already_reminded({item[0] for item in items}, kind)

    sent = 0
    for source_key, event_date, user, text, url in items:
        fingerprint = (source_key, event_date, user.pk)
        if fingerprint in seen:
            continue
        try:
            with transaction.atomic():
                ReminderLog.objects.create(
                    source_key=source_key, event_date=event_date,
                    user=user, kind=kind)
                Notification.objects.create(user=user, text=text[:220], url=url)
        except IntegrityError:
            # Two runs overlapping. The unique constraint is the arbiter, and
            # losing the race is the correct outcome, not an error.
            continue
        seen.add(fingerprint)
        sent += 1
    return sent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _overrides_for(events):
    from .models import EventOccurrence

    recurring = [event.pk for event in events if event.is_recurring]
    if not recurring:
        return {}
    grouped = {}
    for override in EventOccurrence.objects.filter(event_id__in=recurring):
        grouped.setdefault(override.event_id, {})[override.original_date] = override
    return grouped


def _recipients(event):
    """Attendees plus the organiser, deduplicated, active only."""
    people = {user.pk: user for user in event.attendees.all() if user.is_active}
    organiser = event.created_by
    if organiser is not None and organiser.is_active:
        people.setdefault(organiser.pk, organiser)
    return list(people.values())


def _start_datetime(event, effective, override, now):
    """When this occurrence actually starts, honouring an override's times."""
    from .events import ALL_DAY_ANCHOR

    start_time = (override.start_time if override and override.start_time
                  else event.start_time) or ALL_DAY_ANCHOR
    naive = datetime.combine(effective, start_time)
    if timezone.is_aware(now):
        return timezone.make_aware(naive, now.tzinfo)
    return naive


def _user_for(event):
    """The person an alert about `event` belongs to, or None."""
    if not event.user_id:
        return None
    from django.contrib.auth import get_user_model

    return (get_user_model().objects
            .filter(pk=event.user_id, is_active=True).first())
