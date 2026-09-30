"""Asking a client to sign off on work, and recording what they said.

Views stay thin: every state change an approval can go through — sent,
reminded, opened, approved, changes requested, withdrawn — happens here, inside
a transaction, with its event written in the same breath. The calendar entry
follows along, so the client's calendar and the team's planner never disagree
with the approval about where a post stands:

    sent               -> activity "Awaiting client approval"
    approved           -> activity "Approved by client"
    changes requested  -> activity "In progress"
    withdrawn          -> activity back to whatever it was before

The activity is only moved if it is still sitting on "Awaiting client
approval". Someone who has already marked the post Done by hand has told us
something more recent than the approval can, and it isn't overwritten.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import F, Max
from django.urls import reverse
from django.utils import timezone

from core.mailer import absolute_url, deliverable, send
from core.models import notify

from .models import (ApprovalAsset, ApprovalEvent, ApprovalRecipient,
                     ApprovalRequest, ClientActivity, ClientReviewer)

logger = logging.getLogger(__name__)

Status = ApprovalRequest.Status
Event = ApprovalEvent.Kind
ActivityStatus = ClientActivity.Status

MAX_FILES = 10
MAX_LINKS = 10


# ---------------------------------------------------------------------------
# the remembered browser
# ---------------------------------------------------------------------------
#
# A signed cookie, scoped to the reviewer's own path (/r/<token>/). It holds the
# reviewer's id and device epoch — no secret of its own, but it can't be forged
# without SECRET_KEY, it can't be replayed against a different reviewer, and it
# stops matching the moment the epoch is bumped or the token replaced.

DEVICE_SALT = "livo.client-review-device"


def _cookie_name(reviewer):
    return f"dvr{reviewer.pk}"


def remember_seconds():
    return getattr(settings, "CLIENT_REVIEW_REMEMBER_DAYS", 30) * 24 * 60 * 60


def device_is_trusted(request, reviewer):
    value = request.get_signed_cookie(
        _cookie_name(reviewer), default=None, salt=DEVICE_SALT,
        max_age=remember_seconds())
    return value == f"{reviewer.pk}:{reviewer.device_epoch}"


def trust_device(response, request, reviewer):
    response.set_signed_cookie(
        _cookie_name(reviewer), f"{reviewer.pk}:{reviewer.device_epoch}",
        salt=DEVICE_SALT, max_age=remember_seconds(),
        path=reverse("client_review:inbox", args=[reviewer.token]),
        secure=request.is_secure(), httponly=True, samesite="Lax")
    return response


# ---------------------------------------------------------------------------
# reviewers
# ---------------------------------------------------------------------------

def reviewer_for(client, *, name, email, contact=None):
    """The reviewer row for this address at this client, created if new.

    Adding somebody to a request is an explicit decision by the team, so it
    also switches a previously revoked reviewer back on.
    """
    email = (email or "").strip().lower()
    name = (name or "").strip()[:150]
    reviewer, created = ClientReviewer.objects.get_or_create(
        client=client, email=email,
        defaults={"name": name or email.split("@")[0], "contact": contact})
    if not created:
        changed = []
        if name and reviewer.name != name:
            reviewer.name = name
            changed.append("name")
        if contact is not None and reviewer.contact_id != contact.pk:
            reviewer.contact = contact
            changed.append("contact")
        if not reviewer.is_active:
            reviewer.is_active = True
            changed.append("is_active")
        if changed:
            reviewer.save(update_fields=changed + ["updated_at"])
    return reviewer


def parse_links(raw):
    """"Label | https://…" or a bare URL, one per line. Returns (links, error)."""
    links = []
    validate = URLValidator(schemes=["http", "https"])
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        label, _, url = line.rpartition("|") if "|" in line else ("", "", line)
        label, url = label.strip()[:200], url.strip()
        if url and "://" not in url and "." in url.split("/")[0]:
            url = f"https://{url}"
        try:
            validate(url)
        except ValidationError:
            return [], f"“{line[:60]}” isn't a full link — it should start with https://"
        if len(url) > 500:
            return [], "One of the links is too long to store."
        links.append((label, url))
    if len(links) > MAX_LINKS:
        return [], f"Add at most {MAX_LINKS} links."
    return links, ""


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------

def create_request(activity, *, reviewers, message="", content="", respond_by=None,
                   uploads=(), links=(), carried=(), actor=None):
    """Put a new version in front of `reviewers`. Emails are sent separately
    (`send_request_emails`) so a slow mail server never holds the row locks."""
    with transaction.atomic():
        activity = ClientActivity.objects.select_for_update().get(pk=activity.pk)
        requests = ApprovalRequest.objects.filter(activity=activity)
        open_requests = list(requests.select_for_update().filter(status=Status.PENDING))
        version = (requests.aggregate(v=Max("version"))["v"] or 0) + 1

        if open_requests:
            status_before = open_requests[0].status_before
        elif activity.status == ActivityStatus.APPROVAL:
            status_before = ActivityStatus.PLANNED
        else:
            status_before = activity.status

        approval = ApprovalRequest.objects.create(
            activity=activity, version=version, message=message.strip(),
            content=content.strip(), respond_by=respond_by,
            status_before=status_before, created_by=actor)

        for previous in open_requests:
            previous.status = Status.WITHDRAWN
            previous.save(update_fields=["status", "updated_at"])
            ApprovalEvent.objects.create(
                request=previous, kind=Event.WITHDRAWN, actor=actor,
                note=f"Replaced by version {version}.")

        ApprovalRecipient.objects.bulk_create(
            [ApprovalRecipient(request=approval, reviewer=reviewer)
             for reviewer in reviewers])

        position = 0
        for asset in carried:
            # The same stored file, referenced again — not a copy on disk.
            ApprovalAsset.objects.create(
                request=approval, file=asset.file.name if asset.file else "",
                url=asset.url, name=asset.name, size=asset.size, position=position)
            position += 1
        for upload in uploads:
            ApprovalAsset.objects.create(
                request=approval, file=upload, name=(upload.name or "")[:200],
                size=upload.size or 0, position=position)
            position += 1
        for label, url in links:
            ApprovalAsset.objects.create(request=approval, url=url, name=label,
                                         position=position)
            position += 1

        if activity.status != ActivityStatus.APPROVAL:
            activity.status = ActivityStatus.APPROVAL
            activity.save(update_fields=["status", "updated_at"])

        ApprovalEvent.objects.create(
            request=approval, kind=Event.SENT, actor=actor,
            note="To " + ", ".join(reviewer.name for reviewer in reviewers))
    return approval


def _agency():
    try:
        from core.models import AgencySettings
        return AgencySettings.load()
    except Exception:  # branding must never stop an email
        return None


def send_request_emails(approval, *, request=None, actor=None, reminder=False,
                        only=None):
    """Email every active reviewer on `approval` (or just the pks in `only`).

    Returns how many messages reached the mail backend.
    """
    activity = approval.activity
    agency = _agency()
    agency_name = getattr(agency, "agency_name", "") or "Livo Digital"
    asker = approval.created_by or actor
    if reminder:
        subject = f"Reminder: “{activity.title}” is waiting for your approval"
    elif approval.version > 1:
        subject = f"Updated for your approval: {activity.title}"
    else:
        subject = f"Approval needed: {activity.title}"

    now = timezone.now()
    sent, names = 0, []
    recipients = approval.recipients.select_related("reviewer")
    for recipient in recipients:
        reviewer = recipient.reviewer
        if not reviewer.is_active or not reviewer.email:
            continue
        if only is not None and reviewer.pk not in only:
            continue
        ok = send(
            subject=subject,
            template="client_calendar/email/approval_request",
            context={
                "reviewer": reviewer, "approval": approval, "activity": activity,
                "client": activity.project.client, "agency": agency,
                "agency_name": agency_name, "asker": asker, "reminder": reminder,
                "assets": list(approval.assets.all()),
                "url": absolute_url(reviewer.review_url(approval), request),
            },
            to=[reviewer.email],
            reply_to=[asker.email] if deliverable(asker) else (),
            failure_note=f"approval {approval.pk} to reviewer {reviewer.pk}")
        if ok:
            ApprovalRecipient.objects.filter(pk=recipient.pk).update(
                email_count=F("email_count") + 1, last_emailed_at=now)
            sent += 1
            names.append(reviewer.name)
    if reminder and sent:
        ApprovalEvent.objects.create(request=approval, kind=Event.REMINDED,
                                     actor=actor, note="To " + ", ".join(names))
    return sent


def send_code_email(reviewer, code, *, request=None):
    agency = _agency()
    return send(
        subject=f"{code} is your code to review work from "
                f"{getattr(agency, 'agency_name', '') or 'Livo Digital'}",
        template="client_calendar/email/approval_code",
        context={
            "reviewer": reviewer, "code": code, "agency": agency,
            "agency_name": getattr(agency, "agency_name", "") or "Livo Digital",
            "minutes": round(getattr(settings, "CLIENT_REVIEW_CODE_TTL_SECONDS", 900) / 60),
        },
        to=[reviewer.email],
        failure_note=f"review code to reviewer {reviewer.pk}")


# ---------------------------------------------------------------------------
# the client's side
# ---------------------------------------------------------------------------

def record_open(approval, reviewer, *, ip=None):
    now = timezone.now()
    ClientReviewer.objects.filter(pk=reviewer.pk).update(last_seen_at=now)
    deliveries = ApprovalRecipient.objects.filter(request=approval, reviewer=reviewer)
    deliveries.update(last_opened_at=now)
    # Conditional update, so two tabs opening at once log one "opened", not two.
    first = deliveries.filter(first_opened_at__isnull=True).update(first_opened_at=now)
    if first and approval.is_pending:
        ApprovalEvent.objects.create(request=approval, kind=Event.OPENED,
                                     reviewer=reviewer, ip_address=ip)


def decide(approval, reviewer, *, approve, feedback="", ip=None):
    """Record the reviewer's answer. Returns (approval, recorded).

    `recorded` is False when somebody else answered first (or the team
    withdrew it) — the row is locked and re-read, so two reviewers pressing
    the button in the same second can't both win.
    """
    with transaction.atomic():
        locked = ApprovalRequest.objects.select_for_update().get(pk=approval.pk)
        if locked.status != Status.PENDING:
            return locked, False
        locked.status = Status.APPROVED if approve else Status.CHANGES
        locked.decided_by = reviewer
        locked.decided_at = timezone.now()
        locked.feedback = (feedback or "").strip()[:5000]
        locked.save(update_fields=["status", "decided_by", "decided_at",
                                   "feedback", "updated_at"])

        activity = ClientActivity.objects.select_for_update().get(pk=locked.activity_id)
        if activity.status == ActivityStatus.APPROVAL:
            activity.status = (ActivityStatus.APPROVED if approve
                               else ActivityStatus.IN_PROGRESS)
            activity.save(update_fields=["status", "updated_at"])

        ApprovalEvent.objects.create(
            request=locked, kind=Event.APPROVED if approve else Event.CHANGES,
            reviewer=reviewer, note=locked.feedback, ip_address=ip)
    return locked, True


def team_for(approval):
    """Who hears about the client's answer: whoever asked, and whoever planned
    the activity. Falls back to the project's members when neither is known."""
    people = {}
    for person in (approval.created_by, approval.activity.created_by):
        if person is not None and person.is_active:
            people[person.pk] = person
    if not people:
        for person in approval.activity.project.members.filter(is_active=True):
            people[person.pk] = person
    return list(people.values())


def notify_team(approval, *, request=None):
    reviewer = approval.decided_by
    activity = approval.activity
    approved = approval.status == Status.APPROVED
    who = reviewer.name if reviewer else "The client"
    verb = "approved" if approved else "asked for changes on"
    people = team_for(approval)
    notify(people, f"{who} ({activity.project.client.name}) {verb} “{activity.title}”"[:220],
           url=approval.get_absolute_url())

    url = absolute_url(approval.get_absolute_url(), request)
    for person in people:
        if not deliverable(person):
            continue
        send(subject=f"{'Approved' if approved else 'Changes requested'}: {activity.title}",
             template="client_calendar/email/approval_decided",
             context={"person": person, "approval": approval, "activity": activity,
                      "client": activity.project.client, "reviewer": reviewer,
                      "approved": approved, "url": url},
             to=[person.email],
             reply_to=[reviewer.email] if reviewer else (),
             failure_note=f"approval {approval.pk} decision to user {person.pk}")


# ---------------------------------------------------------------------------
# the team's side
# ---------------------------------------------------------------------------

def withdraw(approval, *, actor=None):
    with transaction.atomic():
        locked = ApprovalRequest.objects.select_for_update().get(pk=approval.pk)
        if locked.status != Status.PENDING:
            return locked, False
        locked.status = Status.WITHDRAWN
        locked.save(update_fields=["status", "updated_at"])
        activity = ClientActivity.objects.select_for_update().get(pk=locked.activity_id)
        if activity.status == ActivityStatus.APPROVAL:
            activity.status = locked.status_before or ActivityStatus.PLANNED
            activity.save(update_fields=["status", "updated_at"])
        ApprovalEvent.objects.create(request=locked, kind=Event.WITHDRAWN, actor=actor)
    return locked, True


def delete_files(assets):
    """Remove stored files that no other asset still points at.

    A new version re-references the previous version's files rather than
    copying them, so a file is only garbage once nothing refers to it.
    """
    assets = [asset for asset in assets if asset.file]
    if not assets:
        return
    names = {asset.file.name for asset in assets}
    still_used = set(ApprovalAsset.objects
                     .filter(file__in=names)
                     .exclude(pk__in=[asset.pk for asset in assets])
                     .values_list("file", flat=True))
    for asset in assets:
        if asset.file.name not in still_used:
            try:
                asset.file.delete(save=False)
            except Exception:
                logger.exception("Could not delete approval file %s", asset.file.name)
