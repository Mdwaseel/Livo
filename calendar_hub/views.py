"""Calendar screens.

Four read views (month / week / day / agenda), a day panel loaded on demand, an
event editor, a drag-and-drop endpoint and an `.ics` export. Views stay thin on
purpose: parse the window and the filters, ask `services` for a shape, render.
All the layout arithmetic is testable without a request, and every query into
another app lives behind a source adapter.

Every screen shares one URL grammar — `?date=` anchors the period, `?employee=`
/ `?department=` / `?project=` / `?client=` / `?kind=` narrow it, `?q=` searches
— so a filter survives switching between month and agenda. The resource planner
established that grammar; this follows it rather than inventing a second one.
"""
import json
from datetime import date, datetime, time

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import log_activity, notify
from core.tenancy import workspace_for_new

from . import emails, selectors, services, sources, sync
from .events import KINDS, CalendarQuery
from .models import CalendarEvent, EventOccurrence
from .permissions import (calendar_required, can_create, can_delete, can_edit,
                          can_manage, can_view, create_required)

MODES = ("month", "week", "day", "agenda")


# ---------------------------------------------------------------------------
# request parsing
# ---------------------------------------------------------------------------

def _parse_date(raw, fallback=None):
    try:
        return date.fromisoformat((raw or "").strip())
    except (ValueError, AttributeError):
        return fallback


def _parse_time(raw):
    try:
        return time.fromisoformat((raw or "").strip())
    except (ValueError, AttributeError):
        return None


def _parse_int(raw):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _window(request, mode):
    return services.Window(
        anchor=_parse_date(request.GET.get("date"), timezone.localdate()),
        mode=mode)


def _query(request, window):
    """The five filters plus the search, validated.

    Unknown category keys are dropped rather than passed through — an
    unrecognised `?kind=` would otherwise silently match nothing and read as an
    empty calendar.
    """
    kinds = frozenset(
        value for value in request.GET.getlist("kind") if value in KINDS)
    return CalendarQuery(
        start=window.grid_start,
        end=window.grid_end,
        viewer=request.user,
        employee_id=_parse_int(request.GET.get("employee")),
        department_id=_parse_int(request.GET.get("department")),
        project_id=_parse_int(request.GET.get("project")),
        client_id=_parse_int(request.GET.get("client")),
        kinds=kinds,
        search=(request.GET.get("q") or "").strip()[:100],
    )


def _querystring(request, **overrides):
    """Round-trip the current filters into a link, so the period stepper, the
    view switcher and the export button all keep what's on screen."""
    from urllib.parse import urlencode

    params = []
    for name in ("employee", "department", "project", "client", "q"):
        if name in overrides:
            continue
        value = (request.GET.get(name) or "").strip()
        if value:
            params.append((name, value))
    if "kind" not in overrides:
        params.extend(("kind", value) for value in request.GET.getlist("kind")
                      if value in KINDS)
    params.extend((name, value) for name, value in overrides.items() if value)
    return urlencode(params)


def _base_context(request, window, query, events):
    return {
        "window": window,
        "query": query,
        "today": timezone.localdate(),
        "options": selectors.filter_options(request.user),
        "kinds": services.visible_kinds(request.user),
        # The legend only offers kinds this viewer can actually encounter —
        # a colour key for a colour they will never see is noise, and it also
        # advertises the existence of data they aren't cleared for.
        "visible_kind_keys": {kind.key
                              for kind in services.visible_kinds(request.user)},
        "kind_counts": services.counts_by_kind(events),
        "selected_kinds": list(query.kinds),
        "movable": services.movable_sources(request.user),
        "can_create": can_create(request.user),
        "can_edit": can_edit(request.user),
        "next_up": services.next_up(events, today=timezone.localdate()),
        "overdue": services.overdue(events),
        "querystring": _querystring(request),
        "total": len(events),
    }


# ---------------------------------------------------------------------------
# 1-4. the four views
# ---------------------------------------------------------------------------

@login_required
@calendar_required
def month(request):
    window = _window(request, "month")
    query = _query(request, window)
    grid = services.month_grid(window, query)
    context = _base_context(request, window, query, grid["events"])
    context.update({"weeks": grid["weeks"], "mode": "month"})
    return render(request, "calendar_hub/month.html", context)


@login_required
@calendar_required
def week(request):
    window = _window(request, "week")
    query = _query(request, window)
    grid = services.week_grid(window, query)
    context = _base_context(request, window, query, grid["events"])
    context.update({"columns": grid["columns"], "mode": "week"})
    return render(request, "calendar_hub/week.html", context)


@login_required
@calendar_required
def day(request):
    window = _window(request, "day")
    query = _query(request, window)
    detail = services.day_detail(window, query)
    context = _base_context(request, window, query, detail["events"])
    context.update({"detail": detail, "mode": "day"})
    return render(request, "calendar_hub/day.html", context)


@login_required
@calendar_required
def agenda(request):
    """A paginated list rather than a grid.

    Paged by fortnight rather than by event count: a page that stops halfway
    through a Tuesday looks like missing data, and "the next two weeks" is a
    period somebody can hold in their head.
    """
    window = _window(request, "agenda")
    query = _query(request, window)
    listing = services.agenda(window, query)
    context = _base_context(request, window, query, listing["events"])
    context.update({"days": listing["days"], "mode": "agenda",
                    "page_days": services.AGENDA_PAGE_DAYS})
    return render(request, "calendar_hub/agenda.html", context)


# ---------------------------------------------------------------------------
# lazy day panel
# ---------------------------------------------------------------------------

@login_required
@calendar_required
def day_panel(request, on):
    """One day's entries, fetched when a month cell is opened.

    The month grid renders at most four chips per cell; the rest arrive here.
    Rendering all of them up front would mean a page carrying several hundred
    hidden nodes on a busy month to save a request nobody may ever make.
    """
    anchor = _parse_date(on)
    if anchor is None:
        raise Http404("Not a date.")
    window = services.Window(anchor=anchor, mode="day")
    query = _query(request, window).replace(start=anchor, end=anchor)
    detail = services.day_detail(window, query)
    return render(request, "calendar_hub/_day_panel.html", {
        "detail": detail,
        "movable": services.movable_sources(request.user),
        "today": timezone.localdate(),
        "querystring": _querystring(request),
    })


# ---------------------------------------------------------------------------
# drag-and-drop
# ---------------------------------------------------------------------------

@login_required
@require_POST
def move(request):
    """Reschedule whatever was dragged, through the app that owns it.

    The permission checked is the *source's*, never the calendar's: a task chip
    needs `tasks.edit`, a milestone `milestones.edit`, a meeting `calendar.edit`.
    Every refusal comes back as JSON so a drag handler never has to interpret an
    HTML error page.
    """
    # Two gates before anything is parsed. `calendar.view` because a drag is an
    # action on the calendar and somebody who can't open it has no business
    # posting to it; then "can you move *anything*", so a read-only viewer gets
    # one clear refusal rather than a per-source one.
    if not can_view(request.user):
        return JsonResponse(
            {"ok": False, "error": "You don't have access to the calendar."},
            status=403)
    if not any(services.movable_sources(request.user).values()):
        return JsonResponse(
            {"ok": False, "error": "You don't have permission to move anything "
                                   "on the calendar."}, status=403)

    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Malformed request."},
                            status=400)

    source = sources.get_source(payload.get("source"))
    if source is None:
        return JsonResponse({"ok": False, "error": "Unknown item."}, status=400)
    if not source.can_view(request.user):
        return JsonResponse({"ok": False, "error": "Unknown item."}, status=404)

    object_id = _parse_int(payload.get("id"))
    new_date = _parse_date(payload.get("date"))
    if object_id is None or new_date is None:
        return JsonResponse({"ok": False, "error": "Invalid date."}, status=400)
    occurrence_date = _parse_date(payload.get("occurrence"))

    try:
        _, message = source.move(request.user, object_id, new_date,
                                 occurrence_date=occurrence_date)
    except PermissionDenied as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=403)
    except LookupError as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=404)
    except (ValueError, ValidationError) as error:
        return JsonResponse({"ok": False, "error": _first_message(error)},
                            status=400)

    return JsonResponse({"ok": True, "date": new_date.isoformat(),
                         "message": message})


def _first_message(error):
    if isinstance(error, ValidationError):
        return error.messages[0]
    return str(error)


# ---------------------------------------------------------------------------
# the calendar's own events
# ---------------------------------------------------------------------------

@login_required
@calendar_required
def event_detail(request, pk):
    event = selectors.event_or_none(pk, request.user)
    if event is None:
        # A private event somebody isn't in must be indistinguishable from one
        # that doesn't exist; a 403 here would confirm it is real.
        raise Http404("No such event.")
    return render(request, "calendar_hub/event_detail.html", {
        "event": event,
        "attendees": event.attendees.all(),
        "overrides": selectors.overrides_for(event),
        "can_manage": can_manage(request.user, event),
        "can_delete": can_delete(request.user),
    })


@login_required
@create_required
def event_create(request):
    if request.method == "POST":
        event = CalendarEvent(created_by=request.user)
        return _save_event(request, event, is_new=True)
    anchor = _parse_date(request.GET.get("date"), timezone.localdate())
    return render(request, "calendar_hub/event_form.html",
                  _form_context(request, CalendarEvent(start_date=anchor),
                                is_new=True))


@login_required
@calendar_required
def event_edit(request, pk):
    event = selectors.event_or_none(pk, request.user)
    if event is None:
        raise Http404("No such event.")
    if not can_manage(request.user, event):
        messages.error(request, "You don't have permission to change this event.")
        return redirect(event)
    if request.method == "POST":
        return _save_event(request, event, is_new=False)
    return render(request, "calendar_hub/event_form.html",
                  _form_context(request, event, is_new=False))


@login_required
@require_POST
def event_delete(request, pk):
    # Through the visibility-scoped lookup, not a bare pk: holding
    # `calendar.delete` must not let somebody remove a private event they were
    # never able to see — the deletion would confirm it existed.
    event = selectors.event_or_none(pk, request.user)
    if event is None:
        raise Http404("No such event.")
    if not (can_delete(request.user) or event.created_by_id == request.user.pk):
        messages.error(request, "You don't have permission to remove this event.")
        return redirect(event)
    title = event.title
    attendees = list(event.attendees.all())
    # Built before the row goes: the cancellation has to name the event it is
    # cancelling, and after `delete()` there is nothing left to read it from.
    # See `emails.Invite` for why building and sending are two steps.
    notice = emails.build_invite(event, change="cancelled",
                                 actor=request.user, request=request)
    told = list(attendees)
    if event.created_by_id and event.created_by_id not in {u.pk for u in told}:
        told.append(event.created_by)

    event.delete()
    log_activity(request.user, "deleted event", title)
    if told:
        notify(told, f"“{title}” was cancelled", "", exclude=request.user)
        if notice:
            notice.send_to(told, actor=request.user)
    messages.success(request, f"“{title}” removed.")
    return redirect("calendar_hub:month")


def _form_context(request, event, *, is_new):
    return {
        "event": event,
        "is_new": is_new,
        "options": selectors.filter_options(request.user),
        "people": selectors.attendee_choices(),
        "selected_attendees": ([] if is_new
                               else list(event.attendees.values_list("pk", flat=True))),
        "kind_choices": CalendarEvent.Kind.choices,
        "frequency_choices": CalendarEvent.Frequency.choices,
        "visibility_choices": CalendarEvent.Visibility.choices,
        "weekday_choices": [(1, "Mon"), (2, "Tue"), (3, "Wed"), (4, "Thu"),
                            (5, "Fri"), (6, "Sat"), (7, "Sun")],
        "selected_weekdays": set(event.weekday_numbers) if event.weekdays else set(),
    }


def _save_event(request, event, *, is_new):
    """Hand-parsed POST, matching the house style (`employees.views.edit_hr`,
    `resource_planner.views.capacity_edit`) — this project doesn't use
    ModelForms, and introducing one here would make the calendar the odd module
    out for no gain."""
    post = request.POST
    # Read before a single field is overwritten. Telling somebody already on the
    # meeting that it moved means knowing where it was.
    before = None if is_new else emails.snapshot(event)
    event.kind = (post.get("kind") if post.get("kind") in CalendarEvent.Kind.values
                  else CalendarEvent.Kind.MEETING)
    event.title = (post.get("title") or "").strip()[:200]
    event.description = post.get("description", "")
    event.location = post.get("location", "").strip()[:200]
    event.meeting_url = post.get("meeting_url", "").strip()
    event.start_date = _parse_date(post.get("start_date"))
    event.end_date = _parse_date(post.get("end_date"))
    event.start_time = _parse_time(post.get("start_time"))
    event.end_time = _parse_time(post.get("end_time"))

    event.frequency = (post.get("frequency")
                       if post.get("frequency") in CalendarEvent.Frequency.values
                       else CalendarEvent.Frequency.NONE)
    event.interval = _parse_int(post.get("interval")) or 1
    event.weekdays = ("".join(sorted(set(post.getlist("weekdays"))))
                      if event.frequency == CalendarEvent.Frequency.WEEKLY else "")
    event.repeat_until = _parse_date(post.get("repeat_until"))
    event.repeat_count = _parse_int(post.get("repeat_count"))

    event.project_id = _parse_int(post.get("project"))
    event.client_id = _parse_int(post.get("client"))
    event.department_id = _parse_int(post.get("department"))
    if is_new:
        # Stamped once, at creation. Re-deriving it on every edit would let an
        # event migrate between partitions when a home admin flips the switcher
        # and fixes a typo.
        event.workspace = workspace_for_new(request.user)
    event.visibility = (post.get("visibility")
                        if post.get("visibility") in CalendarEvent.Visibility.values
                        else CalendarEvent.Visibility.AGENCY)
    reminder = post.get("reminder_minutes", "")
    event.reminder_minutes = _parse_int(reminder) if reminder.strip() else None

    if not event.title:
        messages.error(request, "An event needs a title.")
        return _redisplay(request, event, is_new)
    if event.start_date is None:
        messages.error(request, "An event needs a start date.")
        return _redisplay(request, event, is_new)

    try:
        event.full_clean(exclude=["created_by", "client"])
    except ValidationError as error:
        messages.error(request, "; ".join(
            message for group in error.message_dict.values() for message in group))
        return _redisplay(request, event, is_new)

    event.save()
    attendees = [pk for pk in
                 (_parse_int(value) for value in request.POST.getlist("attendees"))
                 if pk]
    previous = set() if is_new else set(event.attendees.values_list("pk", flat=True))
    event.attendees.set(attendees)

    log_activity(request.user, "created event" if is_new else "updated event",
                 event.title, f"{event.start_date}")

    # Only people who weren't already invited get an invitation — an edit to the
    # location should not re-invite everybody.
    current = list(event.attendees.all())
    newly_invited = [user for user in current if user.pk not in previous]
    if newly_invited:
        notify(newly_invited,
               f"You're invited: “{event.title}” on {event.start_date:%d %b}",
               event.get_absolute_url(), exclude=request.user)
        emails.send_event_invite(event, newly_invited, change="invited",
                                 actor=request.user, request=request)

    # Everyone already on it hears only when something they'd have to rearrange
    # their day around actually moved — see emails.MATERIAL_FIELDS. The
    # organiser is included because an event they called can be edited by
    # somebody else.
    if not is_new and emails.changed_materially(before, event):
        already_on = [user for user in current if user.pk in previous]
        if event.created_by_id and event.created_by_id not in {
                user.pk for user in already_on}:
            already_on.append(event.created_by)
        if already_on:
            notify(already_on,
                   f"“{event.title}” changed — now {event.start_date:%d %b}",
                   event.get_absolute_url(), exclude=request.user)
            emails.send_event_invite(event, already_on, change="updated",
                                     actor=request.user, request=request)

    messages.success(request, "Event saved." if not is_new else "Event scheduled.")
    return redirect(event)


def _redisplay(request, event, is_new):
    context = _form_context(request, event, is_new=is_new)
    context["selected_attendees"] = [
        _parse_int(value) for value in request.POST.getlist("attendees")]
    context["selected_weekdays"] = {
        int(value) for value in request.POST.getlist("weekdays")
        if value in "1234567" and value}
    return render(request, "calendar_hub/event_form.html", context)


# ---------------------------------------------------------------------------
# occurrence exceptions
# ---------------------------------------------------------------------------

@login_required
@require_POST
def occurrence_cancel(request, pk):
    """Skip one instance of a recurring series without touching the rule."""
    event = selectors.event_or_none(pk, request.user)
    if event is None:
        raise Http404("No such event.")
    if not can_manage(request.user, event):
        messages.error(request, "You don't have permission to change this event.")
        return redirect(event)
    on = _parse_date(request.POST.get("date"))
    if on is None:
        messages.error(request, "Which occurrence?")
        return redirect(event)
    EventOccurrence.objects.update_or_create(
        event=event, original_date=on,
        defaults={"is_cancelled": True, "moved_to": None})
    log_activity(request.user, "cancelled occurrence", event.title, str(on))
    messages.success(request,
                     f"The {on:%d %b} occurrence of “{event.title}” was skipped.")
    return redirect(event)


@login_required
@require_POST
def occurrence_restore(request, pk, on):
    event = selectors.event_or_none(pk, request.user)
    if event is None:
        raise Http404("No such event.")
    if not can_manage(request.user, event):
        messages.error(request, "You don't have permission to change this event.")
        return redirect(event)
    anchor = _parse_date(on)
    EventOccurrence.objects.filter(event=event, original_date=anchor).delete()
    messages.success(request, "Occurrence restored to the series.")
    return redirect(event)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

@login_required
@calendar_required
def ics_feed(request, mode="agenda"):
    """The window on screen as an `.ics` file.

    A download rather than a subscribable URL, and that is a security decision
    rather than an oversight: a subscription link has to authenticate without a
    session, which means a bearer token in a URL that would hand the agency's
    whole calendar to anyone it is forwarded to. Issuing revocable per-user feed
    tokens is the right way to do it and is a decision for whoever owns the
    deployment, not a default to slip in. The file below opens in Google
    Calendar, Outlook and Apple Calendar today.
    """
    if mode not in MODES:
        raise Http404("Unknown period.")
    window = services.Window(
        anchor=_parse_date(request.GET.get("date"), timezone.localdate()),
        mode=mode)
    query = _query(request, window)
    events = services.collect(query)

    body = sync.to_ics(events, stamp=datetime.utcnow())
    response = HttpResponse(body, content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="{sync.filename_for(window)}"')
    log_activity(request.user, "exported calendar",
                 f"{window.start} to {window.end}", f"{len(events)} entries")
    return response


# ---------------------------------------------------------------------------
# convenience
# ---------------------------------------------------------------------------

@login_required
@calendar_required
def today(request):
    """`/calendar/today/` — the link people bookmark."""
    return redirect(
        f"{_url('day')}?date={timezone.localdate().isoformat()}")


def _url(name):
    from django.urls import reverse

    return reverse(f"calendar_hub:{name}")
