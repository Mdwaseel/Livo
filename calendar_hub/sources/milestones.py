"""Project milestones, read live from `projects.Milestone`."""
from django.utils import timezone

from core.models import log_activity
from core.tenancy import scope
from projects.access import visible_projects
from projects.models import Milestone, Project

from ..events import Event
from .base import EventSource, register, search_filter


@register
class MilestoneSource(EventSource):
    key = "milestone"
    kinds = ("milestone",)
    module = "milestones"
    # A milestone belongs to a project, and so to a client, but has no assignee
    # and no department of its own. Filtering the calendar by employee therefore
    # hides milestones rather than showing all of them — see `sources_for`.
    supports = frozenset({"project", "client"})
    move_action = "edit"
    move_noun = "milestones"

    def fetch(self, query):
        queryset = (Milestone.objects
                    .filter(due_date__gte=query.start, due_date__lte=query.end)
                    .select_related("project", "project__client")
                    .order_by("due_date", "order", "id"))
        if query.project_id:
            queryset = queryset.filter(project_id=query.project_id)
        if query.client_id:
            queryset = queryset.filter(project__client_id=query.client_id)
        queryset = search_filter(queryset, query.search, "title", "project__name")
        queryset = queryset.filter(
            project__in=visible_projects(Project.objects.all(), query.viewer))

        today = timezone.localdate()
        for milestone in queryset:
            done = milestone.status == Milestone.Status.DONE
            yield Event(
                key=f"milestone:{milestone.pk}",
                kind="milestone",
                title=milestone.title,
                start_date=milestone.due_date,
                url=milestone.project.get_absolute_url(),
                detail=f"{milestone.project.name} · {milestone.project.client.name}",
                status_label=milestone.get_status_display(),
                is_overdue=not done and milestone.due_date < today,
                project_id=milestone.project_id,
                project_name=milestone.project.name,
                client_id=milestone.project.client_id,
                client_name=milestone.project.client.name,
                source=self.key,
                object_id=milestone.pk,
                is_movable=True,
            )

    def move(self, user, object_id, new_date, *, occurrence_date=None):
        self._require_move(user)
        # Workspace scope, not just the module permission: `can_move` proved
        # the viewer may reschedule things, never that this thing is theirs.
        milestone = (scope(Milestone.objects, user, path="project__workspace")
                     .filter(pk=object_id)
                     .select_related("project").first())
        if milestone is None:
            raise LookupError("That milestone no longer exists.")
        previous = milestone.due_date
        if previous == new_date:
            return milestone, ""
        milestone.due_date = new_date
        # Not `apply_status` — the due date moving says nothing about whether
        # the milestone is done, and `completed_on` is that model's own business.
        milestone.save(update_fields=["due_date", "updated_at"])
        log_activity(user, "rescheduled milestone",
                     f"{milestone.title} · {milestone.project.name}",
                     f"{previous or 'no date'} → {new_date}")
        return milestone, f"“{milestone.title}” moved to {new_date:%d %b}."
