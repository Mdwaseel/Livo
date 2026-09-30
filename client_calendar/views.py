"""The team's side: plan a client's month, and hand them the link."""
from datetime import date, time

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.db.models import Count, Max, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import log_activity
from core.uploads import is_image, process_upload

from . import access, approvals, services, updates
from .models import (FAMILIES, ApprovalAsset, ApprovalRequest, ClientActivity,
                     ClientCalendarLink, ClientReviewer, ClientUpdate)
from .views_approvals import _names, _people

Status = ClientActivity.Status


def _waiting_approvals(user, **filters):
    activities = access.visible_activities(
        ClientActivity.objects.filter(project__is_archived=False, **filters), user)
    return ApprovalRequest.objects.filter(
        activity__in=activities, status=ApprovalRequest.Status.PENDING).count()


def _deny(request):
    messages.error(request, "You don't have access to client calendars.")
    return redirect("core:dashboard")


def _planner_url(client, day=None):
    url = reverse("client_calendar:planner", args=[client.pk])
    if day:
        # The fragment opens that day's sheet, so after saving you land looking
        # at the thing you just added rather than hunting for it.
        url += f"?date={day:%Y-%m-%d}#d-{day:%Y-%m-%d}"
    return url


# ---------------------------------------------------------------------------
# index + planner
# ---------------------------------------------------------------------------

@login_required
def index(request):
    if not access.can_view(request.user):
        return _deny(request)
    today = timezone.localdate()
    start, end = today.replace(day=1), services.month_end(today)

    clients = list(access.plannable_clients(request.user)
                   .select_related("calendar_link").order_by("name"))
    # One grouped query for every client's month, not one per row.
    stats = (access.visible_activities(
                ClientActivity.objects.filter(date__gte=start, date__lte=end,
                                              project__is_archived=False),
                request.user)
             .values("project__client")
             .annotate(total=Count("id"),
                       done=Count("id", filter=Q(status=Status.DONE)),
                       approval=Count("id", filter=Q(status=Status.APPROVAL))))
    by_client = {row["project__client"]: row for row in stats}

    rows = [{
        "client": client,
        "stats": by_client.get(client.pk, {}),
        # A reverse one-to-one with no row raises an AttributeError subclass,
        # which getattr turns back into None.
        "link": getattr(client, "calendar_link", None),
    } for client in clients]
    return render(request, "client_calendar/index.html", {
        "rows": rows, "month": start,
        "shared": sum(1 for row in rows if row["link"] and row["link"].is_active),
        "approvals_waiting": _waiting_approvals(request.user),
    })


@login_required
def planner(request, client_pk):
    if not access.can_view(request.user):
        return _deny(request)
    client = get_object_or_404(access.plannable_clients(request.user), pk=client_pk)
    projects = access.plannable_projects(request.user).filter(client=client).order_by("name")
    base = access.visible_activities(
        ClientActivity.objects.filter(project__client=client,
                                      project__is_archived=False),
        request.user).prefetch_related(Prefetch(
            # Newest first, so `ClientActivity.latest_approval` is index 0.
            "approval_requests",
            queryset=ApprovalRequest.objects.select_related("decided_by")
            .order_by("-version"),
            to_attr="approval_list"))

    context = services.build(base, request.GET, projects=projects)
    link = ClientCalendarLink.objects.filter(client=client).first()
    context.update({
        "client": client,
        "link": link,
        "share_url": request.build_absolute_uri(link.get_absolute_url()) if link else "",
        "can_plan": access.can_plan(request.user),
        "can_share": access.can_share(request.user),
        "approvals_waiting": _waiting_approvals(request.user, project__client=client),
        "update_people": [{
            **person,
            "frequency": person["reviewer"].update_frequency if person["reviewer"] else "OFF",
            "last_sent": person["reviewer"].last_update_email_at if person["reviewer"] else None,
            "unsubscribed": person["unsubscribed"],
        } for person in _people(client)],
        "update_frequencies": ClientReviewer.Updates.choices,
        "mode": "planner",
    })
    return render(request, "client_calendar/planner.html", context)


# ---------------------------------------------------------------------------
# activity form
# ---------------------------------------------------------------------------

def _kind_groups():
    labels = dict(ClientActivity.Kind.choices)
    return [(label, [(kind, labels[kind]) for kind in kinds])
            for label, kinds in FAMILIES.values()]


def _form_values(activity):
    return {
        "project": str(activity.project_id or ""),
        "kind": activity.kind,
        "platform": activity.platform,
        "status": activity.status,
        "date": activity.date.isoformat() if activity.date else "",
        "time": activity.time.strftime("%H:%M") if activity.time else "",
        "title": activity.title,
        "details": activity.details,
        "link": activity.link,
        "show_to_client": activity.show_to_client,
    }


def _notify_state(values, people):
    """What the "Email the client about this now" section shows.

    A fresh form preselects whoever already gets update emails (or the primary
    contact), with the box itself unticked — sending is always a choice. A
    re-shown POST keeps exactly what was picked.
    """
    if hasattr(values, "getlist"):
        return {"on": values.get("notify") == "on",
                "people": set(values.getlist("notify_people")),
                "emails": values.get("notify_emails", ""),
                "note": values.get("notify_note", "")}
    chosen = {p["value"] for p in people
              if p["reviewer"] and not p["unsubscribed"]
              and p["reviewer"].update_frequency != ClientReviewer.Updates.OFF}
    if not chosen:
        chosen = {p["value"] for p in people if p["primary"] and not p["unsubscribed"]}
    return {"on": False, "people": chosen, "emails": "", "note": ""}


def _notify_targets(post, people):
    """The people to email now, as [(name, email, contact)], or an error."""
    if post.get("notify") != "on":
        return [], ""
    if post.get("show_to_client") != "on":
        return [], ("This activity is hidden from the client, so there's nothing to tell them. "
                    "Tick “Show on the client's calendar”, or untick the email.")
    by_value = {p["value"]: p for p in people}
    targets = {}
    for value in post.getlist("notify_people"):
        person = by_value.get(value)
        if person and not person["unsubscribed"]:
            targets[person["email"]] = (person["name"], person["email"], person["contact"])
    emails, invalid = updates.split_emails(post.get("notify_emails", ""))
    if invalid:
        return [], f"“{invalid[0]}” isn't a valid email address."
    for email in emails:
        targets.setdefault(email, ("", email, None))
    if not targets:
        return [], "Choose who to email, or untick “Email the client about this now”."
    if len(targets) > 25:
        return [], "Email at most 25 people at once."
    return list(targets.values()), ""


def _send_notice(request, activity, targets, since_id):
    """Send the "about this now" email after a save. Returns a sentence for the
    success message, or ""."""
    if not targets:
        return ""
    client = activity.project.client
    reviewers = [updates.person_for(client, name=name, email=email, contact=contact)
                 for name, email, contact in targets]
    skipped = [r for r in reviewers if r.unsubscribed_at]
    changes = list(ClientUpdate.objects.filter(activity=activity, pk__gt=since_id))
    sent = updates.send_activity_notice(
        activity, reviewers, changes=changes, note=request.POST.get("notify_note", ""),
        actor=request.user, request=request)
    parts = []
    if sent:
        parts.append(f" Emailed {_names(sent)}.")
    if skipped:
        parts.append(f" Not emailed {_names(skipped)}, who turned off update emails.")
    failed = len(reviewers) - len(sent) - len(skipped)
    if failed:
        parts.append(f" {failed} email{'s' if failed != 1 else ''} couldn't be sent — check the mail settings.")
    return "".join(parts)


def _whatsapp_text(activity, request):
    link = ClientCalendarLink.objects.filter(client=activity.project.client, is_active=True).first()
    lines = [f"{activity.client_status_label}: {activity.title}",
             f"{activity.date:%a %d %b}" + (f", {activity.time:%I:%M %p}".replace(" 0", " ") if activity.time else "")]
    if activity.link:
        lines.append(activity.link)
    if link:
        lines.append(f"Your calendar: {request.build_absolute_uri(link.get_absolute_url())}")
    return "\n".join(lines)


def _render_form(request, client, activity, projects, *, values, errors=None):
    people = _people(client)
    return render(request, "client_calendar/activity_form.html", {
        "notify_people": people,
        "notify": _notify_state(values, people),
        "wa_text": _whatsapp_text(activity, request) if activity.pk and activity.show_to_client else "",
        "client": client,
        "activity": activity,
        "is_new": activity.pk is None,
        "projects": projects,
        "kind_groups": _kind_groups(),
        "platforms": ClientActivity.Platform.choices,
        "statuses": Status.choices,
        "form": values,
        "errors": errors or {},
        "latest_approval": (activity.approval_requests.select_related("decided_by")
                            .order_by("-version").first() if activity.pk else None),
    }, status=400 if errors else 200)


def _apply(request, activity, projects, extra_errors=None):
    """Validate the POST onto `activity` and save it. Returns {field: message}.

    Hand-parsed, like every other form in the app, and never raising: a
    malformed date or a stray choice comes back as a sentence next to the field
    it belongs to.
    """
    post, errors = request.POST, {}

    project_raw = post.get("project", "")
    project = projects.filter(pk=project_raw).first() if project_raw.isdigit() else None
    if project is None:
        errors["project"] = "Choose which project this belongs to."

    kind = post.get("kind", "")
    if kind not in ClientActivity.Kind.values:
        errors["kind"] = "Choose what kind of activity this is."
    platform = post.get("platform", "")
    if platform and platform not in ClientActivity.Platform.values:
        errors["platform"] = "Choose a platform from the list."
    status = post.get("status", "")
    if status not in Status.values:
        errors["status"] = "Choose a status."

    try:
        on = date.fromisoformat(post.get("date", "").strip())
    except ValueError:
        errors["date"] = "Pick a date."
        on = None
    at = None
    if post.get("time", "").strip():
        try:
            at = time.fromisoformat(post["time"].strip())
        except ValueError:
            errors["time"] = "Use a time like 18:30, or leave it blank."

    title = post.get("title", "").strip()[:160]
    if not title:
        errors["title"] = "Give it a title the client will recognise."
    link = post.get("link", "").strip()
    if link:
        try:
            URLValidator(schemes=["http", "https"])(link)
        except ValidationError:
            errors["link"] = "That isn't a full link — it should start with https://"

    upload = request.FILES.get("preview")
    if upload:
        if not is_image(upload):
            errors["preview"] = "The preview has to be an image — JPG, PNG or WebP."
        else:
            try:
                upload = process_upload(upload)
            except ValidationError as error:
                errors["preview"] = error.messages[0]

    errors.update(extra_errors or {})
    if errors:
        return errors

    activity.project = project
    activity.kind, activity.platform, activity.status = kind, platform, status
    activity.date, activity.time = on, at
    activity.title, activity.details, activity.link = title, post.get("details", "").strip(), link
    activity.show_to_client = post.get("show_to_client") == "on"
    if upload:
        if activity.preview:
            activity.preview.delete(save=False)
        activity.preview = upload
    elif post.get("remove_preview") == "on" and activity.preview:
        activity.preview.delete(save=False)
        activity.preview = ""
    activity.save()
    return {}


@login_required
def activity_create(request, client_pk):
    if not access.can_view(request.user):
        return _deny(request)
    client = get_object_or_404(access.plannable_clients(request.user), pk=client_pk)
    if not access.can_plan(request.user):
        messages.error(request, "You can view this calendar but not add to it.")
        return redirect(_planner_url(client))
    projects = access.plannable_projects(request.user).filter(client=client).order_by("name")

    today = timezone.localdate()
    activity = ClientActivity(created_by=request.user,
                              date=services.parse_anchor(request.GET.get("date"), today))
    wanted = request.GET.get("project", "")
    preselected = projects.filter(pk=wanted).first() if wanted.isdigit() else None
    if preselected is None and len(projects) == 1:
        preselected = projects[0]
    if preselected is not None:
        # Only ever assigned a real project: a non-null foreign key refuses
        # None even on an unsaved instance, which is what made "Add activity"
        # fail for any client with more than one project.
        activity.project = preselected

    if request.method == "POST":
        targets, notify_error = _notify_targets(request.POST, _people(client))
        since_id = ClientUpdate.objects.aggregate(last=Max("pk"))["last"] or 0
        errors = _apply(request, activity, projects,
                        extra_errors={"notify": notify_error} if notify_error else None)
        if errors:
            return _render_form(request, client, activity, projects,
                                values=request.POST, errors=errors)
        log_activity(request.user, "planned client activity",
                     f"{activity.title} · {client.name}", f"{activity.date:%d %b %Y}")
        notice = _send_notice(request, activity, targets, since_id)
        messages.success(request, f"Added “{activity.title}” on {activity.date:%d %b}.{notice}")
        if "add_another" in request.POST:
            return redirect(reverse("client_calendar:activity_create", args=[client.pk])
                            + f"?date={activity.date:%Y-%m-%d}&project={activity.project_id}")
        return redirect(_planner_url(client, activity.date))
    return _render_form(request, client, activity, projects,
                        values=_form_values(activity))


def _editable(request, pk):
    activity = get_object_or_404(
        access.visible_activities(ClientActivity.objects.select_related(
            "project", "project__client"), request.user),
        pk=pk)
    return activity, activity.project.client


@login_required
def activity_edit(request, pk):
    if not access.can_view(request.user):
        return _deny(request)
    activity, client = _editable(request, pk)
    if not access.can_plan(request.user):
        messages.error(request, "You can view this calendar but not change it.")
        return redirect(_planner_url(client, activity.date))
    projects = access.plannable_projects(request.user).filter(client=client).order_by("name")

    if request.method == "POST":
        targets, notify_error = _notify_targets(request.POST, _people(client))
        since_id = ClientUpdate.objects.aggregate(last=Max("pk"))["last"] or 0
        errors = _apply(request, activity, projects,
                        extra_errors={"notify": notify_error} if notify_error else None)
        if errors:
            return _render_form(request, client, activity, projects,
                                values=request.POST, errors=errors)
        log_activity(request.user, "updated client activity",
                     f"{activity.title} · {client.name}", activity.get_status_display())
        notice = _send_notice(request, activity, targets, since_id)
        messages.success(request, f"Saved “{activity.title}”.{notice}")
        return redirect(_planner_url(client, activity.date))
    return _render_form(request, client, activity, projects,
                        values=_form_values(activity))


@login_required
@require_POST
def activity_delete(request, pk):
    if not access.can_view(request.user):
        return _deny(request)
    activity, client = _editable(request, pk)
    if not access.can_plan(request.user):
        messages.error(request, "You can view this calendar but not change it.")
        return redirect(_planner_url(client, activity.date))
    title, on = activity.title, activity.date
    if activity.preview:
        activity.preview.delete(save=False)
    # Read before the cascade removes the rows that name the files.
    approval_files = list(ApprovalAsset.objects.filter(request__activity=activity))
    updates.record_removed(activity)
    activity.delete()
    approvals.delete_files(approval_files)
    log_activity(request.user, "removed client activity", f"{title} · {client.name}")
    messages.success(request, f"Removed “{title}”.")
    return redirect(reverse("client_calendar:planner", args=[client.pk])
                    + f"?date={on:%Y-%m}")


# ---------------------------------------------------------------------------
# who hears about changes
# ---------------------------------------------------------------------------

@login_required
@require_POST
def update_subscribers(request, client_pk):
    if not access.can_view(request.user):
        return _deny(request)
    client = get_object_or_404(access.plannable_clients(request.user), pk=client_pk)
    back = reverse("client_calendar:planner", args=[client.pk]) + "#updates"
    if not access.can_share(request.user):
        messages.error(request, "Only people who can edit clients can choose who gets update emails.")
        return redirect(back)

    frequencies = ClientReviewer.Updates.values
    changed = []
    for person in _people(client):
        frequency = request.POST.get(f"freq_{person['value']}", "")
        if frequency not in frequencies:
            continue
        reviewer = person["reviewer"]
        if person["unsubscribed"]:
            continue  # their choice, not ours to override
        if reviewer is None:
            if frequency == ClientReviewer.Updates.OFF:
                continue
            reviewer = updates.person_for(client, name=person["name"], email=person["email"],
                                          contact=person["contact"])
        if reviewer.update_frequency != frequency:
            updates.set_frequency(reviewer, frequency)
            changed.append(reviewer.name)

    emails, invalid = updates.split_emails(request.POST.get("new_email", ""))
    if invalid:
        messages.error(request, f"“{invalid[0]}” isn't a valid email address.")
        return redirect(back)
    frequency = request.POST.get("new_frequency", ClientReviewer.Updates.SOON)
    if frequency not in frequencies or frequency == ClientReviewer.Updates.OFF:
        frequency = ClientReviewer.Updates.SOON
    skipped = []
    for email in emails:
        name = request.POST.get("new_name", "") if len(emails) == 1 else ""
        reviewer = updates.person_for(client, name=name, email=email)
        if reviewer.unsubscribed_at:
            skipped.append(reviewer.email)
            continue
        updates.set_frequency(reviewer, frequency)
        changed.append(reviewer.name)
    if skipped:
        messages.warning(request, f"{', '.join(skipped)} turned off update emails themselves, "
                                  "so they weren't added back.")

    if changed:
        log_activity(request.user, "changed client update emails", client.name, ", ".join(changed))
        messages.success(request, "Update emails saved.")
    else:
        messages.info(request, "Nothing changed.")
    return redirect(back)


# ---------------------------------------------------------------------------
# the share link
# ---------------------------------------------------------------------------

@login_required
@require_POST
def link_action(request, client_pk):
    if not access.can_view(request.user):
        return _deny(request)
    client = get_object_or_404(access.plannable_clients(request.user), pk=client_pk)
    back = reverse("client_calendar:planner", args=[client.pk]) + "#share"
    if not access.can_share(request.user):
        messages.error(request, "Only people who can edit clients can share their calendar.")
        return redirect(back)

    action = request.POST.get("action", "")
    link = ClientCalendarLink.objects.filter(client=client).first()

    if action == "create" and link is None:
        ClientCalendarLink.objects.create(client=client, created_by=request.user)
        verb, note = "created", "Share link created — copy it and send it to the client."
    elif action == "rotate" and link:
        link.rotate()
        verb, note = "replaced", ("New link created. The old one has stopped working, "
                                  "so send the client the new one.")
    elif action == "disable" and link and link.is_active:
        link.is_active = False
        link.save(update_fields=["is_active", "updated_at"])
        verb, note = "turned off", ("Link turned off. Anyone opening it now sees that "
                                    "it's no longer active.")
    elif action in ("enable", "create") and link and not link.is_active:
        link.is_active = True
        link.save(update_fields=["is_active", "updated_at"])
        verb, note = "turned on", "Link turned back on — the same URL works again."
    else:
        messages.info(request, "Nothing changed.")
        return redirect(back)

    log_activity(request.user, f"{verb} client calendar link", client.name)
    messages.success(request, note)
    return redirect(back)
