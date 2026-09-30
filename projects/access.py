"""Row-level visibility for projects and tasks.

The RBAC matrix answers *what may you do* — open the projects module, edit a
task. It says nothing about *which rows*, so before this module a Developer with
`tasks.view` could read every task in the agency by walking URLs.

Two orthogonal layers now:

    has_perm(user, "projects", "view")      may you open the module at all
    visible_projects(qs, user)              which projects you may see in it

The bypass is itself a permission — `projects.view_all` / `tasks.view_all` —
seeded for Super Admin and Manager and tickable per role in /settings/roles/,
so widening it later is a matrix change rather than a code change.

**The rules**

* A project is visible if you are in `project.members`.
* A task inside a visible project is visible if it is assigned to you, you are
  its reviewer, you created it, or nobody is assigned yet. The reviewer clause
  is not a nicety: without it the review flow deadlocks, since a reviewer is
  notified of a submission they then cannot open. The unassigned clause keeps
  the backlog pickable.

**Workspace scoping.** A third, orthogonal layer sits above both: which
partition the row belongs to (`core.tenancy`). It is applied FIRST and cannot
be bypassed by any permission — `projects.view_all` means "every project in
your world", never "every project in the database". A partner super admin
holds view_all and still sees only their own.

**System context.** Two callers legitimately have no user: the nightly
`send_calendar_reminders` job and `generate_recurring_tasks`. They pass
`user=None`, which means *unfiltered* here — a background job is not a person
being restricted. Every user-facing caller must pass a real user; a `None` that
arrives by accident is a hole, so callers say so explicitly.
"""
from django.db.models import Q

from accounts.permissions import has_perm
from core.tenancy import in_scope, scope


def can_view_all_projects(user):
    """True when this user's roles put every project in reach."""
    return bool(user) and has_perm(user, "projects", "view_all")


def can_view_all_tasks(user):
    return bool(user) and has_perm(user, "tasks", "view_all")


def visible_projects(qs, user):
    """Narrow a Project queryset to what `user` may see.

    `user=None` is system context and returns the queryset untouched.
    """
    if user is None:
        return qs
    qs = scope(qs, user)
    if can_view_all_projects(user):
        return qs
    if not user.is_authenticated:
        return qs.none()
    return qs.filter(members=user).distinct()


def visible_tasks(qs, user):
    """Narrow a Task queryset to what `user` may see.

    Tasks are gated twice — the project must be visible AND the task must be
    theirs — because membership alone would show a Developer every colleague's
    work on a shared project, which is the thing being prevented.
    """
    if user is None:
        return qs
    qs = scope(qs, user, path="project__workspace")
    if can_view_all_tasks(user):
        return qs
    if not user.is_authenticated:
        return qs.none()
    own = Q(assignee=user) | Q(reviewer=user) | Q(created_by=user)
    if can_view_all_projects(user):
        # Every project is already in reach, so membership adds nothing.
        return qs.filter(own | Q(assignee__isnull=True)).distinct()
    # Work that is yours stays visible wherever it lives — being handed a task
    # is itself the grant, and the assignment routes add membership anyway
    # (ensure_member). Only the unassigned backlog depends on membership, which
    # is what stops an outsider browsing a project's to-do column.
    return qs.filter(
        own | Q(assignee__isnull=True, project__members=user)
    ).distinct()


def user_can_see_project(project, user):
    """Single-object form of `visible_projects`, without a query when possible."""
    if user is None:
        return True
    if not in_scope(project.workspace_id, user):
        return False
    if can_view_all_projects(user):
        return True
    if not getattr(user, "is_authenticated", False):
        return False
    return project.members.filter(pk=user.pk).exists()


def user_can_see_task(task, user):
    """Single-object form of `visible_tasks`."""
    if user is None:
        return True
    if not in_scope(task.project.workspace_id, user):
        return False
    if can_view_all_tasks(user):
        return True
    if not getattr(user, "is_authenticated", False):
        return False
    if (task.assignee_id == user.pk
            or task.reviewer_id == user.pk
            or task.created_by_id == user.pk):
        return True
    return (task.assignee_id is None
            and (can_view_all_projects(user)
                 or task.project.members.filter(pk=user.pk).exists()))


def ensure_member(project, user):
    """Put `user` on `project` if they aren't already.

    Called when someone is handed a task: being assigned work on a project you
    cannot open is a dead end — an email and a bell pointing at a 404. Returns
    True when the person was actually added, so callers can mention it.
    """
    if user is None or not getattr(user, "pk", None):
        return False
    if project.members.filter(pk=user.pk).exists():
        return False
    project.members.add(user)
    return True
