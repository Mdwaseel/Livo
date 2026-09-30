"""Project deadlines, read live from `projects.Project.target_end_date`."""
from django.utils import timezone

from core.models import log_activity
from core.tenancy import scope
from projects.access import visible_projects
from projects.models import Project

from ..events import Event
from .base import EventSource, register, search_filter

# A deadline is only a deadline while the work is live. Cancelled projects are
# excluded outright; completed ones keep their marker so a look back at last
# month still shows what landed.
DEAD_STATUSES = (Project.Status.CANCELLED,)


@register
class ProjectDeadlineSource(EventSource):
    key = "project"
    kinds = ("project",)
    module = "projects"
    supports = frozenset({"project", "client"})
    move_action = "edit"
    move_noun = "project deadlines"

    def fetch(self, query):
        queryset = (Project.objects
                    .filter(target_end_date__gte=query.start,
                            target_end_date__lte=query.end,
                            is_archived=False)
                    .exclude(status__in=DEAD_STATUSES)
                    .select_related("client")
                    .order_by("target_end_date", "id"))
        if query.project_id:
            queryset = queryset.filter(pk=query.project_id)
        if query.client_id:
            queryset = queryset.filter(client_id=query.client_id)
        queryset = search_filter(queryset, query.search, "name", "client__name")
        queryset = visible_projects(queryset, query.viewer)

        today = timezone.localdate()
        for project in queryset:
            complete = project.status == Project.Status.COMPLETED
            yield Event(
                key=f"project:{project.pk}",
                kind="project",
                title=f"{project.name} due",
                start_date=project.target_end_date,
                url=project.get_absolute_url(),
                detail=project.client.name,
                status_label=project.get_status_display(),
                is_overdue=not complete and project.target_end_date < today,
                project_id=project.pk,
                project_name=project.name,
                client_id=project.client_id,
                client_name=project.client.name,
                source=self.key,
                object_id=project.pk,
                is_movable=True,
            )

    def move(self, user, object_id, new_date, *, occurrence_date=None):
        self._require_move(user)
        # Workspace scope, not just the module permission: `can_move` proved
        # the viewer may reschedule things, never that this thing is theirs.
        project = (scope(Project.objects, user)
                   .filter(pk=object_id).select_related("client").first())
        if project is None:
            raise LookupError("That project no longer exists.")
        previous = project.target_end_date
        if previous == new_date:
            return project, ""
        # A deadline dragged behind the project's own start date is a typo, not
        # a plan. Refusing it here is cheaper than explaining a negative
        # schedule everywhere downstream.
        if project.start_date and new_date < project.start_date:
            raise ValueError(
                f"{project.name} starts on {project.start_date:%d %b} — "
                "its deadline can't be before that.")
        project.target_end_date = new_date
        project.save(update_fields=["target_end_date", "updated_at"])
        log_activity(user, "rescheduled project deadline", project.name,
                     f"{previous or 'no date'} → {new_date}")
        return project, f"{project.name} now due {new_date:%d %b}."
