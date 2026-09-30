"""Task due dates, read live from `projects.Task`."""
from django.utils import timezone

from core.models import log_activity, notify
from core.tenancy import scope
from projects.access import visible_tasks
from projects.models import Task

from ..events import Event
from .base import EventSource, register, search_filter


@register
class TaskSource(EventSource):
    key = "task"
    kinds = ("task",)
    module = "tasks"
    # A task is the one record that carries all four scope dimensions: an
    # assignee, a department, a project, and through it a client.
    supports = frozenset({"employee", "department", "project", "client"})
    move_action = "edit"
    move_noun = "tasks"
    # A task names its assignee, so the nightly deadline alert has someone to
    # write to. Row-level visibility does not complicate that: the alert only
    # ever goes to the assignee, who can see their own task by definition.
    sends_deadline_alerts = True

    def fetch(self, query):
        queryset = (Task.objects
                    .filter(due_date__gte=query.start, due_date__lte=query.end)
                    .select_related("project", "project__client", "assignee")
                    .order_by("due_date", "order", "id"))
        if query.employee_id:
            queryset = queryset.filter(assignee_id=query.employee_id)
        if query.department_id:
            queryset = queryset.filter(department_id=query.department_id)
        if query.project_id:
            queryset = queryset.filter(project_id=query.project_id)
        if query.client_id:
            queryset = queryset.filter(project__client_id=query.client_id)
        queryset = search_filter(queryset, query.search, "title", "project__name")
        # query.viewer is None for the nightly reminder job — system context,
        # deliberately unfiltered. Every interactive path supplies a real user.
        queryset = visible_tasks(queryset, query.viewer)

        today = timezone.localdate()
        for task in queryset:
            done = task.status == Task.Status.DONE
            yield Event(
                key=f"task:{task.pk}",
                kind="task",
                title=task.title,
                start_date=task.due_date,
                url=task.get_absolute_url(),
                detail=f"{task.project.name} · {task.project.client.name}",
                # Completed tasks stay on the calendar rather than vanishing:
                # "what was due that week" is a question people ask about the
                # past, and a month that empties out behind you is disorienting.
                status_label=(task.get_status_display() if done
                              else "Overdue" if task.due_date < today
                              else task.get_priority_display()),
                is_overdue=not done and task.due_date < today,
                user_id=task.assignee_id,
                user_name=_person(task.assignee),
                department_id=task.department_id,
                project_id=task.project_id,
                project_name=task.project.name,
                client_id=task.project.client_id,
                client_name=task.project.client.name,
                source=self.key,
                object_id=task.pk,
                is_movable=True,
            )

    def move(self, user, object_id, new_date, *, occurrence_date=None):
        self._require_move(user)
        # Workspace scope, not just the module permission: `can_move` proved
        # the viewer may reschedule things, never that this thing is theirs.
        task = (scope(Task.objects, user, path="project__workspace")
                .filter(pk=object_id)
                .select_related("project", "assignee").first())
        if task is None:
            raise LookupError("That task no longer exists.")
        previous = task.due_date
        if previous == new_date:
            return task, ""
        task.due_date = new_date
        task.save(update_fields=["due_date", "updated_at"])
        log_activity(user, "rescheduled task",
                     f"{task.title} · {task.project.name}",
                     f"{previous or 'no date'} → {new_date}")
        # The person who has to do the work is the one whose week just changed.
        if task.assignee_id:
            notify([task.assignee],
                   f"“{task.title}” is now due {new_date:%d %b}",
                   task.get_absolute_url(), exclude=user)
        return task, f"“{task.title}” moved to {new_date:%d %b}."


def _person(user):
    if user is None:
        return ""
    return user.get_full_name() or user.get_username()
