"""Meetings and events — the calendar's own `CalendarEvent` rows, plus the
expansion of any recurrence rules on them.

This is the one source that reads a table this app owns, because it is the one
kind of entry nothing else in the system could hold: a future appointment. See
the module docstring on `calendar_hub.models` for why that earned a table when
the other eight kinds did not.
"""
from django.db.models import Q

from core.models import log_activity, notify
from core.tenancy import scope

from .. import recurrence
from ..events import Event
from ..models import CalendarEvent, EventOccurrence
from .base import EventSource, register, search_filter


@register
class CalendarEventSource(EventSource):
    key = "event"
    kinds = ("meeting", "event")
    module = "calendar"
    # The only source that understands every filter, because a meeting can be
    # about a project, for a client, run by a department, and attended by
    # specific people.
    supports = frozenset({"employee", "department", "project", "client"})
    move_action = "edit"
    move_noun = "meetings"

    def fetch(self, query):
        events = list(self._queryset(query))
        overrides = self._overrides(events)

        for event in events:
            kind = "meeting" if event.kind == CalendarEvent.Kind.MEETING else "event"
            if not query.wants(kind):
                continue
            for occurrence_date, effective, override in recurrence.expand(
                    event, query.start, query.end,
                    overrides=overrides.get(event.pk, {})):
                yield self._to_event(event, kind, occurrence_date, effective,
                                     override)

    # --- reading ---

    def _queryset(self, query):
        """Every row that *could* land in the window, in one query.

        A recurring series starts once and repeats forward, so the window test
        is "starts on or before the window ends, and either has no end or ends
        on or after the window starts" — the same overlap predicate a
        non-recurring event uses, with the series' own end standing in.
        """
        ends_after_start = (
            Q(frequency=CalendarEvent.Frequency.NONE,
              end_date__isnull=True, start_date__gte=query.start)
            | Q(frequency=CalendarEvent.Frequency.NONE,
                end_date__gte=query.start)
            | Q(~Q(frequency=CalendarEvent.Frequency.NONE),
                repeat_until__isnull=True)
            | Q(~Q(frequency=CalendarEvent.Frequency.NONE),
                repeat_until__gte=query.start)
        )
        queryset = (scope(CalendarEvent.objects, query.viewer)
                    .filter(start_date__lte=query.end, is_cancelled=False)
                    .filter(ends_after_start)
                    .select_related("project", "project__client", "client",
                                    "department", "created_by")
                    .prefetch_related("attendees")
                    .order_by("start_date", "start_time", "id"))

        # Private events never leave the database for someone who isn't in them.
        viewer = query.viewer
        viewer_pk = getattr(viewer, "pk", None)
        if viewer_pk is not None:
            queryset = queryset.filter(
                Q(visibility=CalendarEvent.Visibility.AGENCY)
                | Q(created_by_id=viewer_pk)
                | Q(attendees__id=viewer_pk)).distinct()
        else:
            queryset = queryset.filter(visibility=CalendarEvent.Visibility.AGENCY)

        if query.employee_id:
            queryset = queryset.filter(
                Q(attendees__id=query.employee_id)
                | Q(created_by_id=query.employee_id)).distinct()
        if query.department_id:
            queryset = queryset.filter(department_id=query.department_id)
        if query.project_id:
            queryset = queryset.filter(project_id=query.project_id)
        if query.client_id:
            queryset = queryset.filter(client_id=query.client_id)
        return search_filter(queryset, query.search,
                             "title", "description", "location")

    def _overrides(self, events):
        """{event_pk: {original_date: EventOccurrence}} in one query.

        Loaded for every series at once. Asking each series for its own
        exceptions inside the expansion loop is the N+1 this module would
        otherwise walk straight into.
        """
        recurring = [event.pk for event in events if event.is_recurring]
        if not recurring:
            return {}
        grouped = {}
        for override in EventOccurrence.objects.filter(event_id__in=recurring):
            grouped.setdefault(override.event_id, {})[
                override.original_date] = override
        return grouped

    def _to_event(self, event, kind, occurrence_date, effective, override):
        # Only the first day of a multi-day event repeats; a three-day offsite
        # that recurs monthly is three days each month, anchored on the
        # occurrence.
        span = event.duration_days - 1
        start_time = (override.start_time if override and override.start_time
                      else event.start_time)
        end_time = (override.end_time if override and override.end_time
                    else event.end_time)
        key = (recurrence.occurrence_key(event.pk, occurrence_date)
               if event.is_recurring else f"event:{event.pk}")
        attendees = list(event.attendees.all())
        return Event(
            key=key,
            kind=kind,
            title=event.title,
            start_date=effective,
            end_date=effective + _days(span),
            start_time=start_time,
            end_time=end_time,
            url=event.get_absolute_url(),
            detail=event.location or (event.project.name if event.project_id else ""),
            status_label=("Moved" if override and override.moved_to
                          else event.recurrence_label),
            # An event has one organiser but many attendees; `user_id` carries
            # the organiser so a per-person view has something single-valued to
            # group on, and the attendee list travels in the detail.
            user_id=event.created_by_id,
            user_name=_person(event.created_by),
            department_id=event.department_id,
            project_id=event.project_id,
            project_name=event.project.name if event.project_id else "",
            client_id=event.client_id,
            client_name=event.client.name if event.client_id else "",
            source=self.key,
            object_id=event.pk,
            occurrence_date=occurrence_date if event.is_recurring else None,
            external_id=event.external_id,
            is_movable=True,
        )

    # --- writing ---

    def move(self, user, object_id, new_date, *, occurrence_date=None):
        """Move a whole event, or carve one instance out of a series.

        For a recurring series the drag creates an `EventOccurrence` exception
        rather than shifting the rule. Moving the rule would silently relocate
        every future stand-up because somebody dragged one of them, which is
        both surprising and hard to undo.
        """
        self._require_move(user)
        # Workspace scope, not just the module permission: `can_move` proved
        # the viewer may reschedule things, never that this thing is theirs.
        event = (scope(CalendarEvent.objects, user).filter(pk=object_id)
                 .select_related("project").prefetch_related("attendees").first())
        if event is None:
            raise LookupError("That event no longer exists.")
        if not event.visible_to(user):
            raise LookupError("That event no longer exists.")

        if event.is_recurring:
            if occurrence_date is None:
                raise ValueError(
                    "Say which occurrence of a repeating event is moving.")
            if occurrence_date == new_date:
                EventOccurrence.objects.filter(
                    event=event, original_date=occurrence_date).delete()
                return event, ""
            EventOccurrence.objects.update_or_create(
                event=event, original_date=occurrence_date,
                defaults={"moved_to": new_date, "is_cancelled": False})
            log_activity(user, "moved event occurrence", event.title,
                         f"{occurrence_date} → {new_date}")
            message = (f"This occurrence of “{event.title}” moved to "
                       f"{new_date:%d %b}. The rest of the series is unchanged.")
            # Bell only. An updated `.ics` for this event describes the *series*
            # — sending one because a single stand-up moved would reschedule
            # every future stand-up in the recipient's own calendar, which is
            # exactly the surprise the branch above exists to avoid.
            _tell_attendees(event, user, new_date, by_email=False)
            return event, message
        else:
            previous = event.start_date
            if previous == new_date:
                return event, ""
            span = event.duration_days - 1
            event.start_date = new_date
            if event.end_date:
                event.end_date = new_date + _days(span)
            event.save(update_fields=["start_date", "end_date", "updated_at"])
            log_activity(user, "rescheduled event", event.title,
                         f"{previous} → {new_date}")
            message = f"“{event.title}” moved to {new_date:%d %b}."

        _tell_attendees(event, user, new_date)
        return event, message


def _days(count):
    from datetime import timedelta
    return timedelta(days=count)


def _person(user):
    if user is None:
        return ""
    return user.get_full_name() or user.get_username()


def _tell_attendees(event, actor, new_date, *, by_email=True):
    """Bell everyone on the event, and — for a real reschedule — mail them too.

    The organiser is included: an event you called can be dragged to a new day
    by anybody who holds the permission, and finding out from the grid is not
    finding out.
    """
    people = list(event.attendees.all())
    if event.created_by_id and event.created_by_id not in {u.pk for u in people}:
        people.append(event.created_by)
    if not people:
        return
    notify(people, f"“{event.title}” moved to {new_date:%d %b}",
           event.get_absolute_url(), exclude=actor)
    if by_email:
        # Imported here rather than at module scope: `emails` imports `sync`,
        # which the source registry is already midway through loading.
        from .. import emails

        emails.send_event_invite(event, people, change="updated", actor=actor)
