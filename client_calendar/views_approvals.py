"""The team's side of client approvals: ask, chase, withdraw, read the answer."""
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from core.models import log_activity
from core.uploads import max_upload_bytes, max_upload_label, process_upload

from . import access, approvals, updates
from .models import (ApprovalAsset, ApprovalEvent, ApprovalRecipient,
                     ApprovalRequest, ClientActivity, ClientReviewer)

Status = ApprovalRequest.Status


def _deny(request):
    messages.error(request, "You don't have access to client calendars.")
    return redirect("core:dashboard")


def _visible_requests(user):
    activities = access.visible_activities(
        ClientActivity.objects.filter(project__is_archived=False), user)
    return ApprovalRequest.objects.filter(activity__in=activities)


def _activity(request, pk):
    activity = get_object_or_404(
        access.visible_activities(
            ClientActivity.objects.select_related("project", "project__client")
            .filter(project__is_archived=False), request.user),
        pk=pk)
    return activity, activity.project.client


def _names(people):
    names = [person.name for person in people]
    if len(names) <= 2:
        return " and ".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


# ---------------------------------------------------------------------------
# the list
# ---------------------------------------------------------------------------

TABS = (
    ("waiting", "Waiting on client"),
    ("changes", "Changes requested"),
    ("approved", "Approved"),
    ("all", "All"),
)


@login_required
def approvals_index(request):
    if not access.can_view(request.user):
        return _deny(request)

    newer = ApprovalRequest.objects.filter(activity=OuterRef("activity"),
                                           version__gt=OuterRef("version"))
    # A request the client sent back is only still "to do" until version 2
    # goes out; after that it is history, and counting it would nag forever.
    base = (_visible_requests(request.user)
            .annotate(superseded=Exists(newer))
            .exclude(status=Status.WITHDRAWN))
    clients = access.plannable_clients(request.user).order_by("name")
    client_id = request.GET.get("client", "")
    if client_id.isdigit():
        base = base.filter(activity__project__client_id=client_id)

    counts = base.aggregate(
        waiting=Count("id", filter=Q(status=Status.PENDING)),
        changes=Count("id", filter=Q(status=Status.CHANGES, superseded=False)),
        approved=Count("id", filter=Q(status=Status.APPROVED)),
        overdue=Count("id", filter=Q(status=Status.PENDING,
                                     respond_by__lt=timezone.localdate())),
    )

    tab = request.GET.get("tab", "waiting")
    if tab not in dict(TABS):
        tab = "waiting"
    rows = base.select_related("activity", "activity__project",
                               "activity__project__client", "decided_by", "created_by")
    rows = rows.prefetch_related(Prefetch(
        "recipients", queryset=ApprovalRecipient.objects.select_related("reviewer")))
    if tab == "waiting":
        rows = rows.filter(status=Status.PENDING).order_by("respond_by", "created_at")
        # NULL sorts first on some databases and last on others; say it in Python.
        rows = sorted(rows, key=lambda row: (row.respond_by is None,
                                             row.respond_by or date.max, row.created_at))
    elif tab == "changes":
        rows = rows.filter(status=Status.CHANGES, superseded=False).order_by("-decided_at")
    elif tab == "approved":
        rows = rows.filter(status=Status.APPROVED).order_by("-decided_at")[:100]
    else:
        rows = rows.order_by("-created_at")[:150]

    return render(request, "client_calendar/approvals/index.html", {
        "rows": rows,
        "tab": tab,
        "tabs": [(key, label, counts.get(key)) for key, label in TABS],
        "counts": counts,
        "clients": clients,
        "client_id": client_id,
        "today": timezone.localdate(),
    })


# ---------------------------------------------------------------------------
# asking
# ---------------------------------------------------------------------------

def _people(client):
    """Everyone the request could go to: contacts with an email, then anybody
    who has reviewed for this client before without being a contact."""
    reviewers = {r.email: r for r in ClientReviewer.objects.filter(client=client)}
    people, seen = [], set()
    for contact in client.contacts.exclude(email="").order_by("-is_primary", "name"):
        email = contact.email.strip().lower()
        if email in seen:
            continue
        seen.add(email)
        reviewer = reviewers.get(email)
        people.append({
            "value": f"c{contact.pk}", "name": contact.name, "email": email,
            "note": contact.designation, "primary": contact.is_primary,
            "contact": contact, "reviewer": reviewer,
            "unsubscribed": bool(reviewer and reviewer.unsubscribed_at),
        })
    for reviewer in reviewers.values():
        if reviewer.email in seen or not reviewer.is_active:
            continue
        seen.add(reviewer.email)
        people.append({
            "value": f"r{reviewer.pk}", "name": reviewer.name, "email": reviewer.email,
            "note": "", "primary": False, "contact": None, "reviewer": reviewer,
            "unsubscribed": bool(reviewer.unsubscribed_at),
        })
    return people


def _render_form(request, activity, client, *, people, previous, values, errors=None):
    return render(request, "client_calendar/approvals/form.html", {
        "activity": activity,
        "client": client,
        "people": people,
        "previous": previous,
        "carry_options": [asset for asset in previous.assets.all() if asset.file] if previous else [],
        "form": values,
        "errors": errors or {},
        "max_files": approvals.MAX_FILES,
        "max_upload": max_upload_label(),
        "max_bytes": max_upload_bytes(),
        "today": timezone.localdate(),
    }, status=400 if errors else 200)


@login_required
def request_create(request, activity_pk):
    if not access.can_view(request.user):
        return _deny(request)
    activity, client = _activity(request, activity_pk)
    back = reverse("client_calendar:planner", args=[client.pk]) \
        + f"?date={activity.date:%Y-%m-%d}#d-{activity.date:%Y-%m-%d}"
    if not access.can_plan(request.user):
        messages.error(request, "You can view this calendar but not send work for approval.")
        return redirect(back)

    people = _people(client)
    previous = (activity.approval_requests.prefetch_related("assets", "recipients__reviewer")
                .order_by("-version").first())

    if request.method != "POST":
        if previous:
            emails = {r.reviewer.email for r in previous.recipients.all()}
            chosen = [p["value"] for p in people if p["email"] in emails]
        else:
            chosen = [p["value"] for p in people if p["primary"]][:1] \
                or [p["value"] for p in people][:1]
        values = {
            "people": chosen,
            "message": "",
            "content": previous.content if previous else activity.details,
            "respond_by": "",
            "links": "\n".join(
                f"{a.name} | {a.url}" if a.name else a.url
                for a in (previous.assets.all() if previous else []) if a.is_link),
            "carry": [str(a.pk) for a in (previous.assets.all() if previous else []) if a.file],
            "new": [("", "")],
        }
        return _render_form(request, activity, client, people=people,
                            previous=previous, values=values)

    post, errors = request.POST, {}
    by_value = {p["value"]: p for p in people}
    chosen_values = [v for v in post.getlist("people") if v in by_value]
    targets = {}
    for value in chosen_values:
        person = by_value[value]
        targets[person["email"]] = (person["name"], person["email"], person["contact"])

    new_rows = list(zip(post.getlist("new_name"), post.getlist("new_email")))
    for name, raw in new_rows:
        name = name.strip()
        # One box can hold several addresses pasted together; a name only makes
        # sense when there's exactly one of them.
        emails, invalid = updates.split_emails(raw)
        if not name and not emails and not invalid:
            continue
        if invalid or not emails:
            errors["new"] = f"“{(invalid or [name])[0]}” needs a valid email address."
            continue
        for email in emails:
            targets.setdefault(email, ((name if len(emails) == 1 else "") or email.split("@")[0],
                                       email, None))
    if not targets and "new" not in errors:
        errors["people"] = "Choose at least one person to send it to."

    respond_by = None
    if post.get("respond_by", "").strip():
        try:
            respond_by = date.fromisoformat(post["respond_by"].strip())
        except ValueError:
            errors["respond_by"] = "Pick a date, or leave it blank."
        else:
            if respond_by < timezone.localdate():
                errors["respond_by"] = "That date has already passed."

    links, link_error = approvals.parse_links(post.get("links", ""))
    if link_error:
        errors["links"] = link_error

    uploads = request.FILES.getlist("files")
    processed = []
    if len(uploads) > approvals.MAX_FILES:
        errors["files"] = f"Attach at most {approvals.MAX_FILES} files — put the rest in a Drive folder and add its link."
    else:
        for upload in uploads:
            try:
                processed.append(process_upload(upload))
            except ValidationError as error:
                errors["files"] = error.messages[0]
                break

    carried = []
    if previous:
        wanted = set(post.getlist("carry"))
        carried = [a for a in previous.assets.all() if a.file and str(a.pk) in wanted]

    content = post.get("content", "").strip()
    if not (content or processed or links or carried) and "files" not in errors:
        errors["content"] = "Add the copy, a file or a link for them to review."

    if errors:
        values = {
            "people": chosen_values, "message": post.get("message", ""),
            "content": post.get("content", ""), "respond_by": post.get("respond_by", ""),
            "links": post.get("links", ""), "carry": post.getlist("carry"),
            "new": new_rows or [("", "")],
        }
        if uploads:
            errors.setdefault("files_again", "For security, files have to be chosen again after a correction.")
        return _render_form(request, activity, client, people=people,
                            previous=previous, values=values, errors=errors)

    reviewers = [approvals.reviewer_for(client, name=name, email=email, contact=contact)
                 for name, email, contact in targets.values()]
    approval = approvals.create_request(
        activity, reviewers=reviewers, message=post.get("message", ""), content=content,
        respond_by=respond_by, uploads=processed, links=links, carried=carried,
        actor=request.user)
    sent = approvals.send_request_emails(approval, request=request, actor=request.user)

    log_activity(request.user, "sent for client approval",
                 f"{activity.title} · {client.name}",
                 f"Version {approval.version} to {_names(reviewers)}")
    if sent == len(reviewers):
        messages.success(request, f"Sent to {_names(reviewers)}. You'll be notified the moment they answer.")
    elif sent:
        messages.warning(request, f"Sent to {sent} of {len(reviewers)} people — the other emails failed. Use “Resend” to try again.")
    else:
        messages.warning(request, "The request is saved, but the email couldn't be sent. Check the mail settings, then use “Resend”.")
    return redirect(approval.get_absolute_url())


# ---------------------------------------------------------------------------
# one request
# ---------------------------------------------------------------------------

def _request(request, pk):
    return get_object_or_404(
        _visible_requests(request.user).select_related(
            "activity", "activity__project", "activity__project__client",
            "decided_by", "created_by"),
        pk=pk)


@login_required
def request_detail(request, pk):
    if not access.can_view(request.user):
        return _deny(request)
    approval = _request(request, pk)
    activity = approval.activity
    versions = list(activity.approval_requests.select_related("decided_by").order_by("-version"))
    return render(request, "client_calendar/approvals/detail.html", {
        "approval": approval,
        "activity": activity,
        "client": activity.project.client,
        "assets": list(approval.assets.all()),
        "recipients": list(approval.recipients.select_related("reviewer")),
        "history": list(approval.events.select_related("reviewer", "actor")),
        "versions": versions,
        "latest": versions[0] if versions else approval,
        "can_plan": access.can_plan(request.user),
        "Event": ApprovalEvent.Kind,
    })


@login_required
@require_POST
def request_remind(request, pk):
    if not access.can_view(request.user):
        return _deny(request)
    approval = _request(request, pk)
    if not access.can_plan(request.user):
        messages.error(request, "You can view approvals but not send reminders.")
    elif not approval.is_pending:
        messages.info(request, "This request has already been answered or withdrawn.")
    else:
        only = None
        if request.POST.get("reviewer", "").isdigit():
            only = {int(request.POST["reviewer"])}
        sent = approvals.send_request_emails(approval, request=request, actor=request.user,
                                             reminder=True, only=only)
        if sent:
            messages.success(request, f"Reminder sent to {sent} {'person' if sent == 1 else 'people'}.")
        else:
            messages.warning(request, "The reminder couldn't be sent. Check the mail settings and try again.")
    return redirect(approval.get_absolute_url())


@login_required
@require_POST
def request_withdraw(request, pk):
    if not access.can_view(request.user):
        return _deny(request)
    approval = _request(request, pk)
    if not access.can_plan(request.user):
        messages.error(request, "You can view approvals but not withdraw them.")
        return redirect(approval.get_absolute_url())
    approval, done = approvals.withdraw(approval, actor=request.user)
    if done:
        log_activity(request.user, "withdrew client approval request",
                     f"{approval.activity.title} · {approval.client.name}")
        messages.success(request, "Withdrawn. The link in their email now says it's no longer waiting on them.")
    else:
        messages.info(request, "It had already been answered, so there was nothing to withdraw.")
    return redirect(approval.get_absolute_url())


@login_required
@require_POST
def reviewer_access(request, pk):
    """Sign a reviewer's remembered browsers out, or take their access away."""
    if not access.can_view(request.user):
        return _deny(request)
    reviewer = get_object_or_404(
        ClientReviewer.objects.filter(client__in=access.plannable_clients(request.user)),
        pk=pk)
    back = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()},
                                           require_https=request.is_secure()):
        back = reverse("client_calendar:approvals")
    if not access.can_plan(request.user):
        messages.error(request, "You can view approvals but not change who can review.")
        return redirect(back)

    action = request.POST.get("action", "")
    if action == "revoke":
        reviewer.is_active = False
        reviewer.device_epoch += 1
        reviewer.save(update_fields=["is_active", "device_epoch", "updated_at"])
        note = (f"{reviewer.name} can no longer open approvals. Sending them a new "
                "request gives access back.")
    elif action == "signout":
        reviewer.sign_out_devices()
        note = f"{reviewer.name} will need a fresh emailed code on every device."
    else:
        return redirect(back)
    log_activity(request.user, f"{'revoked' if action == 'revoke' else 'reset'} client reviewer access",
                 f"{reviewer.name} · {reviewer.client.name}")
    messages.success(request, note)
    return redirect(back)

