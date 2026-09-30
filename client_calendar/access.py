"""Who may plan, see and share a client's calendar.

No new RBAC module. Every activity belongs to a project, so the project rules
already answer the questions this app asks, and a role administrator who has
tuned the matrix at /settings/roles/ gets the behaviour they configured rather
than a fresh column of checkboxes to rediscover:

* see the planner             -> `projects.view`, on projects you can see
* add, change, remove entries -> `projects.edit`   (delivery roles hold it —
                                 the designer who makes the reel schedules it)
* create or revoke the link   -> `clients.edit`    (handing a client a URL is a
                                 client-relationship act, not a delivery one)

Row visibility goes through `projects.access.visible_projects`, so a Developer
sees activities only on projects they are a member of and a partner workspace
never sees ours.
"""
from accounts.permissions import has_perm
from clients.access import visible_clients
from clients.models import Client
from projects.access import visible_projects
from projects.models import Project


def can_view(user):
    return has_perm(user, "projects", "view")


def can_plan(user):
    return has_perm(user, "projects", "edit")


def can_share(user):
    return has_perm(user, "clients", "edit")


def plannable_projects(user):
    return visible_projects(Project.objects.filter(is_archived=False), user)


def visible_activities(qs, user):
    return qs.filter(project__in=visible_projects(Project.objects.all(), user))


def plannable_clients(user):
    """Clients with at least one live project this viewer can see."""
    return (visible_clients(Client.objects.filter(is_archived=False), user)
            .filter(projects__in=plannable_projects(user))
            .distinct())
