"""Telling a client, by email, what changed on their calendar.

The share link answers "what's the plan?" whenever the client thinks to look.
This answers "has anything changed?" without them having to look — which is the
question a client actually asks ("how will I know when you update it?").

Three decisions shape it:

* **Batched, never one email per edit.** A planner adding twelve posts on a
  Monday morning must produce one email, not twelve. "As things change" waits
  for a quiet spell (CLIENT_UPDATES_QUIET_MINUTES, default 20) after the last
  change, capped so a busy afternoon still sends within
  CLIENT_UPDATES_MAX_WAIT_MINUTES (default 120). Daily and weekly summaries go
  once, in the morning (CLIENT_UPDATES_DIGEST_HOUR, default 9).

* **Collapsed per activity.** A post added, then moved, then renamed inside one
  batch is one line: "newly planned — Thursday". Added and then deleted before
  the email went is no line at all. The email describes where things ended up,
  not the team's working.

* **Only what the client can see.** Nothing hidden from the client is logged,
  and at send time anything that has since been hidden or archived is dropped —
  so a draft briefly made visible by mistake doesn't turn up in an inbox.

Sending runs from `manage.py send_client_updates` on a schedule (every ten
minutes). Each subscriber's `updates_since` cursor only moves once their email
has gone, under a row lock, so overlapping runs can't send the same batch twice.
"""
from __future__ import annotations

import logging
import re
from datetime import time, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from core.mailer import absolute_url, deliverable, send

from .models import (DONE_LABEL, ApprovalRequest, ClientActivity, ClientCalendarLink,
                     ClientReviewer, ClientUpdate)

logger = logging.getLogger(__name__)

Kind = ClientUpdate.Kind
Frequency = ClientReviewer.Updates
Status = ClientActivity.Status

# Order of the sections in the email: the good news first.
SECTIONS = (
    (Kind.DONE, "Published & live"),
    (Kind.ADDED, "Newly planned"),
    (Kind.MOVED, "Rescheduled"),
    (Kind.RESUMED, "Back on the plan"),
    (Kind.POSTPONED, "Postponed"),
    (Kind.REMOVED, "Taken off the calendar"),
)

FREQUENCY_HELP = {
    Frequency.SOON: "A short email soon after we update your calendar. "
                    "Several changes made together arrive as one email.",
    Frequency.DAILY: "One email in the morning, only on days something changed.",
    Frequency.WEEKLY: "One email on Monday morning with the week's changes.",
    Frequency.OFF: "No update emails. You'll still get approval requests.",
}


def _minutes(name, default):
    return timedelta(minutes=getattr(settings, name, default))


def _digest_hour():
    return getattr(settings, "CLIENT_UPDATES_DIGEST_HOUR", 9)


# ---------------------------------------------------------------------------
# recording (called from the post_save signal and the delete view)
# ---------------------------------------------------------------------------

def _log(activity, kind, *, old_date=None, old_time=None):
    project = activity.project
    if project.is_archived:
        return None
    return ClientUpdate.objects.create(
        client_id=project.client_id, activity=activity, activity_ref=activity.pk, kind=kind,
        title=activity.title[:160], activity_kind=activity.kind,
        platform=activity.platform, date=activity.date, time=activity.time,
        old_date=old_date, old_time=old_time)


def record_saved(activity, created):
    """Log what this save changed, if it's something the client would notice.

    Status changes the client causes or is told about another way are left
    out: awaiting approval and approved travel through the approval emails.
    """
    if created:
        if activity.show_to_client:
            _log(activity, Kind.ADDED)
        return
    before = getattr(activity, "_tracked", None)
    if before is None:
        # Built by hand rather than read from the database: nothing to compare.
        return

    was_visible = before.get("show_to_client", activity.show_to_client)
    if not activity.show_to_client:
        if was_visible:
            _log(activity, Kind.REMOVED)
        return
    if not was_visible:
        _log(activity, Kind.ADDED)
        return

    old_status = before.get("status", activity.status)
    if old_status != activity.status:
        if activity.status == Status.DONE:
            _log(activity, Kind.DONE)
        elif activity.status == Status.POSTPONED:
            _log(activity, Kind.POSTPONED)
        elif old_status == Status.POSTPONED:
            _log(activity, Kind.RESUMED)

    old_date = before.get("date", activity.date)
    old_time = before.get("time", activity.time)
    if (old_date, old_time) != (activity.date, activity.time):
        _log(activity, Kind.MOVED, old_date=old_date, old_time=old_time)


def record_removed(activity):
    """Call before deleting an activity."""
    if activity.show_to_client:
        _log(activity, Kind.REMOVED)


# ---------------------------------------------------------------------------
# who gets them
# ---------------------------------------------------------------------------

def person_for(client, *, name, email, contact=None):
    """The reviewer row for this address, created if new.

    Unlike `approvals.reviewer_for` it never switches a revoked reviewer back
    on — choosing how often somebody hears about the calendar is not a
    decision to give them approval access again.
    """
    email = (email or "").strip().lower()
    reviewer, _ = ClientReviewer.objects.get_or_create(
        client=client, email=email,
        defaults={"name": (name or "").strip()[:150] or email.split("@")[0],
                  "contact": contact})
    return reviewer


_EMAIL_SEPARATORS = re.compile(r"[\s,;]+")
_NAMED_ADDRESS = re.compile(r"[^,;<>\n]*<([^<>]+)>")


def split_emails(raw):
    """"a@x.com, b@y.com; c@z.com" → (valid, invalid), lower-cased, de-duplicated.

    Pasted from anywhere — an email's To line, a spreadsheet column, WhatsApp —
    so commas, semicolons, spaces and new lines all separate, and angle brackets
    from "Priya <priya@acme.com>" are dropped.
    """
    valid, invalid, seen = [], [], set()
    # "Priya Shah <priya@acme.com>" — keep the address, drop the display name,
    # so a To line pasted from an email doesn't report "Priya" as a bad address.
    raw = _NAMED_ADDRESS.sub(r" \1 ", raw or "")
    for part in _EMAIL_SEPARATORS.split(raw):
        part = part.strip().strip("<>\"'").lower()
        if not part:
            continue
        try:
            validate_email(part)
        except ValidationError:
            invalid.append(part)
            continue
        if part not in seen:
            seen.add(part)
            valid.append(part)
    return valid, invalid


def set_frequency(reviewer, frequency, *, now=None, by_client=False):
    """Change how often `reviewer` hears about the calendar.

    `by_client` is True only when the person did it themselves; turning emails
    off that way is remembered as an unsubscribe the team can't override.
    """
    if frequency not in Frequency.values:
        raise ValueError(f"Unknown update frequency: {frequency!r}")
    now = now or timezone.now()
    fields = ["update_frequency", "updated_at"]
    if by_client:
        reviewer.unsubscribed_at = now if frequency == Frequency.OFF else None
        fields.append("unsubscribed_at")
    if frequency != Frequency.OFF and (reviewer.update_frequency == Frequency.OFF
                                       or reviewer.updates_since is None):
        reviewer.updates_since = now
        fields.append("updates_since")
    reviewer.update_frequency = frequency
    reviewer.save(update_fields=fields)


# ---------------------------------------------------------------------------
# building the email
# ---------------------------------------------------------------------------

def pending(reviewer):
    if reviewer.updates_since is None:
        return []
    return list(ClientUpdate.objects
                .filter(client_id=reviewer.client_id, created_at__gt=reviewer.updates_since)
                .exclude(announced_to=reviewer)
                .select_related("activity", "activity__project")
                .order_by("created_at", "id"))


def is_due(reviewer, updates, now):
    if not updates:
        return False
    frequency = reviewer.update_frequency
    if frequency == Frequency.SOON:
        return (now - updates[-1].created_at >= _minutes("CLIENT_UPDATES_QUIET_MINUTES", 20)
                or now - updates[0].created_at >= _minutes("CLIENT_UPDATES_MAX_WAIT_MINUTES", 120))

    local = timezone.localtime(now)
    if local.hour < _digest_hour():
        return False
    last = reviewer.last_update_email_at
    sent_today = last is not None and timezone.localtime(last).date() >= local.date()
    if frequency == Frequency.DAILY:
        return not sent_today
    if frequency == Frequency.WEEKLY:
        return local.weekday() == 0 and not sent_today
    return False


_KIND_LABELS = dict(ClientActivity.Kind.choices)
_PLATFORM_LABELS = dict(ClientActivity.Platform.choices)


def collapse(updates):
    """One line per activity, describing where it ended up. See module docstring."""
    groups = {}
    for update in updates:
        key = ("activity", update.activity_ref) if update.activity_ref else ("update", update.pk)
        groups.setdefault(key, []).append(update)

    items = []
    for group in groups.values():
        first, last = group[0], group[-1]
        kinds = [update.kind for update in group]
        activity = last.activity
        moves = [update for update in group if update.kind == Kind.MOVED]

        if last.kind == Kind.REMOVED:
            if first.kind == Kind.ADDED:
                continue  # came and went before anybody was told
            kind, source = Kind.REMOVED, last
        else:
            if (activity is None or not activity.show_to_client
                    or activity.project.is_archived):
                continue
            source = activity
            if first.kind == Kind.ADDED:
                kind = Kind.ADDED
            elif last.kind in (Kind.DONE, Kind.POSTPONED, Kind.RESUMED):
                kind = last.kind
            elif Kind.MOVED in kinds:
                kind = Kind.MOVED
            else:
                kind = last.kind
            # The status moved on again before the email went: say nothing
            # rather than something that is no longer true.
            if kind == Kind.DONE and activity.status != Status.DONE:
                continue
            if kind == Kind.POSTPONED and activity.status != Status.POSTPONED:
                continue
            if kind == Kind.ADDED and activity.status == Status.DONE:
                kind = Kind.DONE

        old_date = moves[0].old_date if moves else None
        old_time = moves[0].old_time if moves else None
        if kind == Kind.MOVED and (old_date, old_time) == (source.date, source.time):
            continue  # moved and moved back

        activity_kind = source.kind if source is activity else source.activity_kind
        items.append({
            "kind": kind,
            "title": source.title,
            "kind_label": _KIND_LABELS.get(activity_kind, ""),
            "platform_label": _PLATFORM_LABELS.get(source.platform, ""),
            "date": source.date,
            "time": source.time,
            "old_date": old_date if kind == Kind.MOVED else None,
            "old_time": old_time if kind == Kind.MOVED else None,
            "done_label": DONE_LABEL.get(activity_kind, "Done"),
            "link": activity.link if (kind == Kind.DONE and activity is not None) else "",
        })
    return items


def sections(items):
    grouped = []
    for kind, label in SECTIONS:
        rows = sorted((item for item in items if item["kind"] == kind),
                      key=lambda item: (item["date"], item["time"] is None, item["time"] or time.min))
        if rows:
            grouped.append({"kind": kind, "label": label, "items": rows})
    return grouped


def coming_up(client, today, days=7):
    return list(ClientActivity.objects
                .filter(project__client=client, project__is_archived=False,
                        show_to_client=True, date__gte=today,
                        date__lt=today + timedelta(days=days))
                .exclude(status__in=(Status.DONE, Status.POSTPONED))
                .order_by("date", "time", "id")[:12])


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------

def _agency():
    try:
        from core.models import AgencySettings
        return AgencySettings.load()
    except Exception:  # branding must never stop an email
        return None


def _deliver(reviewer, link, items, now):
    client = reviewer.client
    agency = _agency()
    agency_name = getattr(agency, "agency_name", "") or "Livo Digital"
    count = len(items)
    unsubscribe = absolute_url(reverse("client_review:unsubscribe", args=[reviewer.token]))
    headers = {}
    if unsubscribe:
        # RFC 8058 one-click unsubscribe: Gmail and Outlook show their own
        # "Unsubscribe" button, which is kinder than a spam report.
        headers = {"List-Unsubscribe": f"<{unsubscribe}>",
                   "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    waiting = 0
    if reviewer.is_active:
        waiting = (ApprovalRequest.objects
                   .filter(status=ApprovalRequest.Status.PENDING,
                           recipients__reviewer=reviewer,
                           activity__project__is_archived=False)
                   .count())
    summary = reviewer.update_frequency in (Frequency.DAILY, Frequency.WEEKLY)
    return send(
        subject=f"{count} update{'s' if count != 1 else ''} to your {client.name} calendar",
        template="client_calendar/email/client_update",
        context={
            "reviewer": reviewer, "client": client, "agency": agency,
            "agency_name": agency_name, "count": count, "sections": sections(items),
            "summary": summary,
            "frequency_label": reviewer.get_update_frequency_display().lower(),
            "coming_up": coming_up(client, timezone.localdate(now)) if summary else [],
            "waiting": waiting,
            "calendar_url": absolute_url(link.get_absolute_url()),
            "approvals_url": absolute_url(reviewer.get_absolute_url()) if waiting else "",
            "prefs_url": absolute_url(reverse("client_review:updates", args=[reviewer.token])),
        },
        to=[reviewer.email],
        reply_to=[agency.email] if getattr(agency, "email", "") else (),
        headers=headers,
        failure_note=f"calendar update to reviewer {reviewer.pk}")


def send_digest(reviewer, *, now=None, dry_run=False):
    """Email `reviewer` their pending changes if they're due. Returns True if an
    email went (or, in a dry run, would have)."""
    now = now or timezone.now()
    with transaction.atomic():
        reviewer = (ClientReviewer.objects.select_for_update()
                    .select_related("client").get(pk=reviewer.pk))
        if reviewer.update_frequency == Frequency.OFF or reviewer.client.is_archived:
            return False
        updates = pending(reviewer)
        if not is_due(reviewer, updates, now):
            return False
        # Updates point people at the calendar. With the link switched off
        # there is nowhere to send them, so the changes wait — they go out,
        # still collapsed, once the link is back on.
        link = ClientCalendarLink.objects.filter(client=reviewer.client, is_active=True).first()
        if link is None:
            return False

        items = collapse(updates)
        cutoff = updates[-1].created_at
        if not items:
            if not dry_run:
                reviewer.updates_since = cutoff
                reviewer.save(update_fields=["updates_since", "updated_at"])
            return False
        if dry_run:
            return True
        if not _deliver(reviewer, link, items, now):
            return False  # cursor stays put, so the next run tries again
        reviewer.updates_since = cutoff
        reviewer.last_update_email_at = now
        reviewer.save(update_fields=["updates_since", "last_update_email_at", "updated_at"])
        return True


# ---------------------------------------------------------------------------
# "Email the client about this now" — one activity, straight away
# ---------------------------------------------------------------------------
#
# The summaries are for the steady drip of planning. Some changes deserve their
# own email the moment they happen — the reel is live, here's the link — and
# the person making the change is the one who knows which. They tick the box on
# the activity form; this sends it, and records who was told so their next
# summary doesn't say it twice.

NOTICE_ORDER = (Kind.DONE, Kind.POSTPONED, Kind.MOVED, Kind.RESUMED, Kind.ADDED, Kind.REMOVED)


def describe(activity, changes):
    """(kind, label, move) for the email: what this save did, most important first."""
    kinds = {change.kind for change in changes}
    kind = next((candidate for candidate in NOTICE_ORDER if candidate in kinds), "")
    label = {
        Kind.DONE: DONE_LABEL.get(activity.kind, "Done"),
        Kind.POSTPONED: "Postponed",
        Kind.MOVED: "Rescheduled",
        Kind.RESUMED: "Back on the plan",
        Kind.ADDED: "Newly planned",
    }.get(kind) or activity.client_status_label
    move = next((change for change in changes if change.kind == Kind.MOVED), None)
    return kind, label, move


def send_activity_notice(activity, reviewers, *, changes=(), note="", actor=None, request=None):
    """Email each reviewer about `activity` now. Returns the reviewers emailed.

    People who unsubscribed themselves are skipped — a message the team chose
    to send is still an email they asked not to get.
    """
    client = activity.project.client
    kind, label, move = describe(activity, changes)
    link = ClientCalendarLink.objects.filter(client=client, is_active=True).first()
    agency = _agency()
    agency_name = getattr(agency, "agency_name", "") or "Livo Digital"
    preview_url = ""
    if link and activity.preview and activity.show_to_client:
        preview_url = absolute_url(
            reverse("client_calendar_public:preview", args=[link.token, activity.pk]), request)
    if deliverable(actor):
        reply_to = [actor.email]
    else:
        reply_to = [agency.email] if getattr(agency, "email", "") else []

    sent = []
    for reviewer in reviewers:
        if reviewer.unsubscribed_at or not reviewer.email:
            continue
        unsubscribe = absolute_url(reverse("client_review:unsubscribe", args=[reviewer.token]), request)
        headers = ({"List-Unsubscribe": f"<{unsubscribe}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"} if unsubscribe else {})
        if send(subject=f"{label}: {activity.title}",
                template="client_calendar/email/activity_notice",
                context={
                    "reviewer": reviewer, "client": client, "activity": activity,
                    "agency": agency, "agency_name": agency_name,
                    "kind": kind, "label": label, "move": move,
                    "note": (note or "").strip()[:500], "actor": actor,
                    "preview_url": preview_url,
                    "calendar_url": absolute_url(link.get_absolute_url(), request) if link else "",
                    "prefs_url": absolute_url(reverse("client_review:updates", args=[reviewer.token]), request),
                },
                to=[reviewer.email], reply_to=reply_to, headers=headers,
                failure_note=f"activity {activity.pk} notice to reviewer {reviewer.pk}"):
            sent.append(reviewer)
    if sent:
        for change in changes:
            change.announced_to.add(*sent)
    return sent


def run(*, now=None, dry_run=False):
    now = now or timezone.now()
    subscribers = list(ClientReviewer.objects
                       .exclude(update_frequency=Frequency.OFF)
                       .filter(client__is_archived=False)
                       .values_list("pk", flat=True))
    sent = 0
    for pk in subscribers:
        try:
            if send_digest(ClientReviewer(pk=pk), now=now, dry_run=dry_run):
                sent += 1
        except Exception:
            logger.exception("Calendar update run failed for reviewer %s", pk)
    return {
        "subscribers": len(subscribers),
        "sent": sent,
        "dry_run": dry_run,
        "site_url_missing": not getattr(settings, "SITE_URL", ""),
    }
