"""RFC 5545 serialisation — the working half of "sync ready".

Google Calendar, Outlook and Apple Calendar all read iCalendar. A `.ics` export
is therefore not a stopgap on the way to a real integration; it is the one
interchange format all three already agree on, and it costs no credentials, no
OAuth consent screen and no token storage.

Written by hand rather than with a dependency. The subset needed is small and
completely specified, the project has no calendar library and adding one to emit
four hundred bytes of text would be the larger decision. What matters is
correctness on the parts that bite:

* **Line folding.** RFC 5545 caps a line at 75 octets and continues with a
  leading space. Long meeting titles are the common case, and an unfolded file
  is rejected outright by Outlook.
* **Escaping.** Commas, semicolons, backslashes and newlines are special inside
  a property value. A project called "Acme, Inc." breaks an unescaped feed.
* **UID stability.** The UID is derived from `Event.key`, so re-importing a feed
  updates the same appointment instead of stacking a second copy of it.
* **All-day encoding.** VALUE=DATE with a DTEND of the day *after* the last day.
  A DTEND equal to DTSTART is a zero-length event, which some clients drop.
"""
from __future__ import annotations

from datetime import timedelta

PRODID = "-//Livo Digital//Agency OS Calendar//EN"
LINE_LIMIT = 75


def to_ics(events, *, name="Livo Digital", stamp=None):
    """A complete VCALENDAR for `events`."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _property("X-WR-CALNAME", name),
    ]
    for event in events:
        lines.extend(_vevent(event, stamp=stamp))
    lines.append("END:VCALENDAR")
    # CRLF, not LF: the spec says so, and Outlook is the client that notices.
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _vevent(event, *, stamp=None):
    lines = ["BEGIN:VEVENT", _property("UID", f"{_uid(event)}@livo")]
    if stamp is not None:
        lines.append(f"DTSTAMP:{stamp:%Y%m%dT%H%M%SZ}")

    if event.is_all_day:
        lines.append(f"DTSTART;VALUE=DATE:{event.start_date:%Y%m%d}")
        # Exclusive end — the day after the last day the event covers.
        lines.append(
            f"DTEND;VALUE=DATE:{event.end_date + timedelta(days=1):%Y%m%d}")
    else:
        lines.append(f"DTSTART:{event.start_datetime:%Y%m%dT%H%M%S}")
        end = event.end_time or event.start_time
        lines.append(
            f"DTEND:{event.end_date:%Y%m%d}T{end:%H%M%S}")

    lines.append(_property("SUMMARY", event.title))
    description = " · ".join(part for part in (event.detail, event.status_label)
                             if part)
    if description:
        lines.append(_property("DESCRIPTION", description))
    # The kind is exported as a category so a subscriber can colour by it in
    # their own client — the colour coding survives the export.
    lines.append(_property("CATEGORIES", event.kind_info.label))
    if event.url:
        lines.append(_property("URL", event.url))
    lines.append("END:VEVENT")
    return lines


def _uid(event):
    # ':' and '@' are legal in a UID but make for an unreadable one, and the
    # '@domain' suffix is added by the caller.
    return event.key.replace(":", "-").replace("@", "-")


def _property(name, value):
    return f"{name}:{_escape(value)}"


def _escape(value):
    """Escape a TEXT property value. Order matters — backslash first, or the
    escapes introduced below would be escaped again."""
    return (str(value)
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\r\n", "\\n")
            .replace("\n", "\\n"))


def _fold(line):
    """Wrap at 75 octets with a leading-space continuation.

    Measured in UTF-8 bytes rather than characters: the limit in the spec is
    octets, and a title with an em-dash or a curly quote in it — which the rest
    of this codebase uses freely — is longer than it looks.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= LINE_LIMIT:
        return line

    pieces, chunk, size = [], [], 0
    for character in line:
        width = len(character.encode("utf-8"))
        # 74 on continuation lines: the leading space counts toward the limit.
        cap = LINE_LIMIT if not pieces else LINE_LIMIT - 1
        if size + width > cap:
            pieces.append("".join(chunk))
            chunk, size = [], 0
        chunk.append(character)
        size += width
    if chunk:
        pieces.append("".join(chunk))
    return "\r\n ".join(pieces)


def filename_for(window):
    """A download name that says what's inside it."""
    return f"livo-calendar-{window.start:%Y-%m-%d}-to-{window.end:%Y-%m-%d}.ics"


# ---------------------------------------------------------------------------
# invitations
# ---------------------------------------------------------------------------
# `to_ics` above serialises the read-only `events.Event` view objects the
# calendar grid is built from — a flattened, already-expanded occurrence with no
# recurrence rule and no attendee list, which is right for "export what's on
# screen" and wrong for "invite these people to this meeting".
#
# An invitation needs the things a grid cell threw away: the RRULE, so a daily
# stand-up lands in Gmail as one repeating appointment instead of nothing; the
# ORGANIZER and ATTENDEE lines, so the recipient's client shows who called it;
# and METHOD:REQUEST, which is what makes Gmail and Outlook render an
# "Add to calendar" strip instead of a file to download and open by hand. So it
# reads the `CalendarEvent` row directly.

# ISO weekday (Mon=1) to the two-letter code RFC 5545 uses, so the model's
# `weekdays` string can become a BYDAY without a second lookup table anywhere.
ICS_WEEKDAYS = {1: "MO", 2: "TU", 3: "WE", 4: "TH", 5: "FR", 6: "SA", 7: "SU"}

FREQUENCIES = {"DAILY": "DAILY", "WEEKLY": "WEEKLY",
               "MONTHLY": "MONTHLY", "YEARLY": "YEARLY"}


def invite_ics(event, *, method="REQUEST", stamp=None, url="",
               organiser=None, attendees=()):
    """A one-event VCALENDAR for `event`, ready to attach to an invitation.

    `method` is REQUEST for a new or changed meeting and CANCEL for one that has
    been called off — a CANCEL with the same UID is what removes the appointment
    from the recipient's calendar rather than leaving a ghost behind.

    Times are floating (no TZID, no trailing Z), matching `to_ics`: the whole
    agency works in one timezone and a floating time is read as local by every
    client, which is the intended meaning. Introducing a VTIMEZONE here without
    a per-user timezone to put in it would be precision the data doesn't have.
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        _property("UID", f"event-{event.pk}@livo"),
        # A bumped SEQUENCE is how a client knows an edit supersedes what it
        # already has. `updated_at` is monotonic per row and already maintained,
        # so it serves without adding a column that could drift out of step.
        f"SEQUENCE:{int(event.updated_at.timestamp()) if event.updated_at else 0}",
        "STATUS:" + ("CANCELLED" if method == "CANCEL" or event.is_cancelled
                     else "CONFIRMED"),
    ]
    if stamp is not None:
        lines.append(f"DTSTAMP:{stamp:%Y%m%dT%H%M%SZ}")

    if event.is_all_day:
        lines.append(f"DTSTART;VALUE=DATE:{event.start_date:%Y%m%d}")
        # Exclusive end — the day after the last day the event covers. A DTEND
        # equal to DTSTART is a zero-length event, which some clients drop.
        lines.append(
            f"DTEND;VALUE=DATE:{event.last_date + timedelta(days=1):%Y%m%d}")
    else:
        lines.append(f"DTSTART:{event.start_date:%Y%m%d}T{event.start_time:%H%M%S}")
        end_time = event.end_time or event.start_time
        lines.append(f"DTEND:{event.last_date:%Y%m%d}T{end_time:%H%M%S}")

    rule = _rrule(event)
    if rule:
        lines.append(rule)

    lines.append(_property("SUMMARY", event.title))
    if event.description:
        lines.append(_property("DESCRIPTION", event.description))
    # The join link goes in LOCATION as well as URL when there is no physical
    # room, because LOCATION is the field phone calendars surface on the lock
    # screen — which is where somebody actually taps it.
    where = event.location or event.meeting_url
    if where:
        lines.append(_property("LOCATION", where))
    if event.meeting_url:
        lines.append(_property("URL", event.meeting_url))
    elif url:
        lines.append(_property("URL", url))
    lines.append(_property("CATEGORIES", event.get_kind_display()))

    if organiser is not None and getattr(organiser, "email", ""):
        lines.append(f"ORGANIZER;CN={_escape(_display(organiser))}:"
                     f"mailto:{organiser.email}")
    for person in attendees:
        if not getattr(person, "email", ""):
            continue
        lines.append(
            f"ATTENDEE;CN={_escape(_display(person))};ROLE=REQ-PARTICIPANT;"
            f"PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{person.email}")

    if event.reminder_minutes:
        lines.extend([
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            _property("DESCRIPTION", event.title),
            f"TRIGGER:-PT{event.reminder_minutes}M",
            "END:VALARM",
        ])

    lines.extend(["END:VEVENT", "END:VCALENDAR"])
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _rrule(event):
    """The RRULE line for a recurring event, or "" when it doesn't repeat."""
    frequency = FREQUENCIES.get(event.frequency)
    if not frequency:
        return ""
    parts = [f"FREQ={frequency}"]
    if event.interval and event.interval > 1:
        parts.append(f"INTERVAL={event.interval}")
    if frequency == "WEEKLY":
        parts.append("BYDAY=" + ",".join(
            ICS_WEEKDAYS[number] for number in event.weekday_numbers))
    if event.repeat_until:
        # UNTIL has to carry the same value type as DTSTART, or the rule is
        # rejected outright rather than merely misread.
        parts.append(f"UNTIL={event.repeat_until:%Y%m%d}"
                     if event.is_all_day
                     else f"UNTIL={event.repeat_until:%Y%m%d}T235959")
    elif event.repeat_count:
        parts.append(f"COUNT={event.repeat_count}")
    return "RRULE:" + ";".join(parts)


def _display(user):
    return user.get_full_name() or user.get_username()
