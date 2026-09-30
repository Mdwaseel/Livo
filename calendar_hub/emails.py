"""Meeting invitations, changes and cancellations, by email.

`reminders.py` already sends "your 3pm starts in an hour" through the in-app
bell. That is the *reminder*, and it fires an hour before the thing happens. It
is not the invitation. Somebody who is put on next Tuesday's client review needs
to hear about it on the day it is scheduled, in the place they keep their
commitments — which is their inbox and their own calendar, not a bell they will
next look at on Tuesday afternoon.

So three moments send mail, and they are genuinely different messages rather
than one message with a changed verb:

* **Invited** — you are on a meeting you weren't on before.
* **Changed** — a meeting you are already on moved, or its details did. Sent
  only when something a person would rearrange their day around actually
  changed (see `MATERIAL_FIELDS`); a typo fixed in the description is not worth
  an email to eight people.
* **Cancelled** — it isn't happening. This one matters most, because the
  failure mode of *not* sending it is somebody sitting in a room alone.

Every message carries a `.ics` attachment built by `sync.invite_ics`, with
METHOD:REQUEST (or CANCEL). That is what makes Gmail and Outlook render an
add-to-calendar strip rather than an attachment to open by hand, and it is what
makes a cancellation actually remove the appointment from the recipient's own
calendar instead of leaving a ghost on it. The recurrence rule travels with it,
so a daily stand-up arrives as one repeating appointment.

One message per recipient, not one message with everybody in `To`. The
attachment names its recipient as the ATTENDEE, and a private one-to-one should
not publish its guest list in a header.

The delivery rules are the ones in `core.mailer`: a mail failure is logged and
swallowed, never raised — the meeting is already in the calendar and visible by
the time any of this runs.
"""
from __future__ import annotations

from datetime import datetime

from django.conf import settings

from core.mailer import absolute_url, deliverable, send

from . import sync

# The fields whose change is worth interrupting someone for. Deliberately not
# every field on the model: `description`, `visibility` and `reminder_minutes`
# are excluded because none of them changes where anybody has to be.
MATERIAL_FIELDS = (
    "title", "start_date", "end_date", "start_time", "end_time",
    "location", "meeting_url", "frequency", "interval", "weekdays",
    "repeat_until", "repeat_count", "is_cancelled",
)

HEADLINES = {
    "invited": "You're invited",
    "updated": "A meeting changed",
    "cancelled": "A meeting was cancelled",
}


def snapshot(event):
    """The values `changed_materially` compares. Take one before saving."""
    return {field: getattr(event, field) for field in MATERIAL_FIELDS}


def changed_materially(before, event):
    """Did anything move that a person would need to rearrange their day for?"""
    if not before:
        return False
    return any(before.get(field) != getattr(event, field)
               for field in MATERIAL_FIELDS)


class Invite:
    """A rendered invitation, built from an event and then sent to people.

    Two steps rather than one because of the cancellation. Deleting the event is
    what makes the cancellation true, and everything this message says — the
    UID, the attendee list, the link — has to be read off the row *before* it
    goes. Building first and sending after means the mail can't go out for a
    delete that then failed, and the delete doesn't have to happen after the
    mail to keep the data available. Every other change uses the same two steps
    because one path through the code is easier to trust than two.
    """

    def __init__(self, *, subject, context, calendar, filename, event_pk):
        self.subject = subject
        self.context = context
        self.calendar = calendar
        self.filename = filename
        self.event_pk = event_pk

    def send_to(self, recipients, *, actor=None):
        """Mail each deliverable recipient. Returns how many were sent."""
        actor_pk = getattr(actor, "pk", None)
        sent = 0
        for person in recipients:
            if not deliverable(person) or person.pk == actor_pk:
                continue
            if send(subject=self.subject,
                    template="calendar_hub/email/event_invite",
                    context={**self.context, "person": person},
                    to=[person.email],
                    # The cancellation carries the same file with METHOD:CANCEL
                    # — that is what clears the appointment from the recipient's
                    # own calendar instead of leaving a ghost on it.
                    attachments=[(self.filename, self.calendar, "text/calendar")],
                    failure_note=f"event {self.event_pk} to user {person.pk}"):
                sent += 1
        return sent


def build_invite(event, *, change="invited", actor=None, request=None):
    """Render the message for `event`, or None when invitations are switched off.

    `change` is "invited", "updated" or "cancelled" — it picks the subject, the
    headline and the iCalendar METHOD, and nothing else differs between them.
    """
    if not getattr(settings, "CALENDAR_INVITE_EMAILS", True):
        return None
    if change not in HEADLINES:
        raise ValueError(f"Unknown change kind: {change!r}")

    organiser = event.created_by
    attendees = list(event.attendees.all())
    url = absolute_url(event.get_absolute_url(), request)
    method = "CANCEL" if change == "cancelled" else "REQUEST"
    verb = {"invited": "", "updated": "Updated: ",
            "cancelled": "Cancelled: "}[change]

    return Invite(
        subject=f"{verb}{event.title} · {event.start_date:%a %d %b}",
        calendar=sync.invite_ics(
            event, method=method, stamp=datetime.utcnow(), url=url,
            organiser=organiser, attendees=attendees),
        filename=_filename(event),
        event_pk=event.pk,
        context={
            "event": event,
            "actor": actor,
            "organiser": organiser,
            "attendees": attendees,
            "project": event.project,
            "client": event.client,
            "department": event.department,
            "change": change,
            "headline": HEADLINES[change],
            "when": when_label(event),
            "url": url,
        },
    )


def send_event_invite(event, recipients, *, change="invited", actor=None,
                      request=None):
    """Build and send in one call — the shorthand for everything but a delete.

    Returns how many messages were sent. Sends nothing when the feature is off
    or when nobody on the list has an address, and never mails the person who
    made the change about their own decision.
    """
    invite = build_invite(event, change=change, actor=actor, request=request)
    return invite.send_to(recipients, actor=actor) if invite else 0


def when_label(event):
    """One line that answers "when do I need to be there?".

    Built here rather than in the template because the four shapes an event can
    take — all-day, timed, multi-day, repeating — do not collapse into a format
    string, and getting "9 Aug, all day" to read correctly for a three-day
    offsite is worth more than template purity.
    """
    day = f"{event.start_date:%A %d %B %Y}"
    if event.spans_days:
        span = f"{day} to {event.last_date:%A %d %B %Y}"
        if event.is_all_day:
            return span
        times = f"{event.start_time:%H:%M}"
        if event.end_time:
            times += f"–{event.end_time:%H:%M}"
        return f"{span}, {times}"
    if event.is_all_day:
        return f"{day}, all day"
    times = f"{event.start_time:%H:%M}"
    if event.end_time:
        times += f"–{event.end_time:%H:%M}"
    return f"{day}, {times}"


def _filename(event):
    """A name the recipient's client will show if it declines to inline it."""
    safe = "".join(character if character.isalnum() else "-"
                   for character in event.title).strip("-").lower() or "meeting"
    return f"{safe[:60]}.ics"
