"""Client calendar entries — posts, stories, ad changes — read live from
`client_calendar.ClientActivity`.

So the team sees the content schedule on the agency calendar beside the
deadlines it depends on, without a second copy of it. Governed by `projects`,
like the planner itself: dragging a reel to Friday is changing the client's
plan, and that is project work, not `calendar.edit`.
"""
from django.urls import reverse
from django.utils import timezone

from client_calendar.models import ClientActivity
from core.models import log_activity
from core.tenancy import scope
from projects.access import visible_projects
from projects.models import Project

from ..events import Event
from .base import EventSource, register, search_filter

OPEN = (ClientActivity.Status.PLANNED, ClientActivity.Status.IN_PROGRESS,
        ClientActivity.Status.APPROVAL, ClientActivity.Status.APPROVED)


@register
class ClientActivitySource(EventSource):
    key = "client_activity"
    kinds = ("client_activity",)
    module = "projects"
    supports = frozenset({"project", "client"})
    move_action = "edit"
    move_noun = "client calendar entries"

    def fetch(self, query):
        queryset = (ClientActivity.objects
                    .filter(date__gte=query.start, date__lte=query.end,
                            project__is_archived=False)
                    .select_related("project", "project__client")
                    .order_by("date", "time", "id"))
        if query.project_id:
            queryset = queryset.filter(project_id=query.project_id)
        if query.client_id:
            queryset = queryset.filter(project__client_id=query.client_id)
        queryset = search_filter(queryset, query.search, "title", "project__name",
                                 "project__client__name")
        queryset = queryset.filter(
            project__in=visible_projects(Project.objects.all(), query.viewer))

        today = timezone.localdate()
        for activity in queryset:
            client = activity.project.client
            detail = " · ".join(part for part in (
                activity.get_kind_display(),
                activity.get_platform_display() if activity.platform else "",
                client.name,
                "" if activity.show_to_client else "hidden from client",
            ) if part)
            yield Event(
                key=f"client_activity:{activity.pk}",
                kind="client_activity",
                title=activity.title,
                start_date=activity.date,
                start_time=activity.time,
                url=(reverse("client_calendar:planner", args=[client.pk])
                     + f"?date={activity.date:%Y-%m-%d}#d-{activity.date:%Y-%m-%d}"),
                detail=detail,
                status_label=activity.get_status_display(),
                is_overdue=activity.status in OPEN and activity.date < today,
                project_id=activity.project_id,
                project_name=activity.project.name,
                client_id=client.pk,
                client_name=client.name,
                source=self.key,
                object_id=activity.pk,
                is_movable=True,
            )

    def move(self, user, object_id, new_date, *, occurrence_date=None):
        self._require_move(user)
        activity = (scope(ClientActivity.objects, user, path="project__workspace")
                    .filter(pk=object_id,
                            project__in=visible_projects(Project.objects.all(), user))
                    .select_related("project").first())
        if activity is None:
            raise LookupError("That client calendar entry no longer exists.")
        previous = activity.date
        if previous == new_date:
            return activity, ""
        activity.date = new_date
        activity.save(update_fields=["date", "updated_at"])
        log_activity(user, "rescheduled client activity",
                     f"{activity.title} · {activity.project.name}",
                     f"{previous} → {new_date}")
        return activity, f"“{activity.title}” moved to {new_date:%d %b}."
