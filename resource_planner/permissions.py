"""Access rules, routed through the existing RBAC engine.

The brief names Managers, Super Admins and Project Managers. Rather than
hardcode those three role names — which would put permissions back in code, the
exact thing the RBAC rewrite removed — a `resource_planning` module key is added
to the catalog and granted to those roles by migration. The matrix at
/settings/roles/ then governs it like everything else, and giving a Team Lead
access later is a checkbox rather than a deploy.

Four gates:

* `resource_planning.view`   — see the planner.
* `resource_planning.edit`   — change capacity profiles.
* `tasks.assign`             — drag a task onto somebody. Reuses the existing
                               key rather than inventing a second answer to
                               "may this person reassign work".
* `leaves.create` / `.edit`  — record leave. Already in the catalog and already
                               held by HR, who own it.
"""
from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect

from accounts.permissions import has_perm

MODULE = "resource_planning"


def can_view(user):
    return has_perm(user, MODULE, "view")


def can_edit_capacity(user):
    return has_perm(user, MODULE, "edit")


def can_assign_tasks(user):
    """Drag-and-drop reassignment is task assignment; it uses the task module's
    own permission so the planner cannot become a way around it."""
    return has_perm(user, "tasks", "assign")


def can_manage_leave(user):
    return has_perm(user, "leaves", "create") or has_perm(user, "leaves", "edit")


def can_approve_leave(user):
    return has_perm(user, "leaves", "approve")


def planner_required(view):
    """Redirect-with-message on denial, matching the app's established UX —
    the sidebar link is already hidden from anyone without access."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not can_view(request.user):
            messages.error(request, "You don't have access to resource planning.")
            return redirect("core:dashboard")
        return view(request, *args, **kwargs)
    return wrapper


def capacity_edit_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not can_view(request.user):
            messages.error(request, "You don't have access to resource planning.")
            return redirect("core:dashboard")
        if not can_edit_capacity(request.user):
            messages.error(request, "You don't have permission to change capacity.")
            return redirect("resource_planner:employees")
        return view(request, *args, **kwargs)
    return wrapper
