"""Emails the projects app sends.

`core.notify` puts a line under the topbar bell, which only reaches someone who
is already in the app. Two events need to reach them wherever they are:

* **Being handed a task.** Work you don't know about is work that doesn't get
  done.
* **Being added to a project.** Membership is what makes a project *visible* at
  all in this system (see `projects.access`), so it is not a label — it is the
  moment a whole area of the app appears for someone. Telling them only via a
  bell they may not look at for a week means a client's work sits unseen.

Two rules shape both:

* **Delivery never breaks the change.** By the time these run the task is saved
  and the membership is granted, so every send is wrapped and logged rather than
  raised. `core.mailer.send` does that; see its docstring.
* **Nobody is emailed about their own decision.** Assigning a task to yourself,
  or adding yourself to a project, is not news; `actor` is compared against the
  recipient and the send is skipped.
"""
from django.conf import settings

from core.mailer import absolute_url, deliverable, send  # noqa: F401 (re-exported)

from .models import Task


def send_task_assigned(task, actor=None, request=None):
    """Email `task.assignee` that the task is now theirs.

    Returns True only when a message was actually handed to the mail backend, so
    callers and tests can tell "sent" from "deliberately skipped".

    Skipped silently when the feature is off, nobody is assigned, the assignee
    has no address on file or is deactivated, or the assignee is the person who
    made the assignment.
    """
    if not getattr(settings, "TASK_ASSIGNMENT_EMAILS", True):
        return False

    assignee = task.assignee
    if not deliverable(assignee):
        return False
    if actor is not None and getattr(actor, "pk", None) == assignee.pk:
        return False

    project = task.project
    return send(
        subject=f"New task: {task.title}",
        template="projects/email/task_assigned",
        context={
            "task": task,
            "assignee": assignee,
            "actor": actor,
            "project": project,
            "client": getattr(project, "client", None),
            "url": absolute_url(task.get_absolute_url(), request),
        },
        to=[assignee.email],
        failure_note=f"task {task.pk} to user {assignee.pk}",
    )


def send_project_member_added(project, person, actor=None, request=None):
    """Email `person` that they now have access to `project`.

    Carries the orientation someone needs to act on it — who the client is, what
    state the project is in, when it is due, who else is on it and how much work
    is already waiting for them — so the mail answers "what is this and what do
    I do?" without a round trip into the app.

    Skipped on the same terms as the assignment email: feature off, nobody to
    write to, or the person added themselves.
    """
    if not getattr(settings, "PROJECT_MEMBER_EMAILS", True):
        return False
    if not deliverable(person):
        return False
    if actor is not None and getattr(actor, "pk", None) == person.pk:
        return False

    # Their own open tasks on this project, not the whole board: "3 tasks are
    # already waiting for you" is actionable, "the project has 47 tasks" is
    # trivia.
    my_open = (project.tasks
               .filter(assignee=person)
               .exclude(status=Task.Status.DONE)
               .count())
    teammates = [user for user in project.members.filter(is_active=True)
                 if user.pk != person.pk]

    return send(
        subject=f"You've been added to {project.name}",
        template="projects/email/project_member_added",
        context={
            "project": project,
            "client": getattr(project, "client", None),
            "person": person,
            "actor": actor,
            "teammates": teammates,
            "my_open_tasks": my_open,
            "url": absolute_url(project.get_absolute_url(), request),
        },
        to=[person.email],
        failure_note=f"project {project.pk} to user {person.pk}",
    )
