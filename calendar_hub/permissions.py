"""Access rules, entirely reused from the existing RBAC engine.

No new permission concept is introduced and no role name appears in this file.
The `calendar` module key has been in `accounts.rbac_seed.MODULES` since the
RBAC rewrite, granted as `view/create/edit` to every working role through
`_BASE` and in full to Super Admin and Manager — a key reserved with no model
behind it, exactly as `leaves` was before the resource planner filled it. This
module fills it. No migration is needed and no matrix is rewritten: the
permissions the agency already granted are the ones that now do something.

Four gates, and one principle worth stating because it shapes the whole module:

* `calendar.view`   — open the calendar.
* `calendar.create` — schedule a meeting or an event.
* `calendar.edit`   — change one, and drag one to another day.
* `calendar.delete` — remove one.

The principle: **`calendar.edit` governs the calendar's own events and nothing
else.** Dragging a task chip to Thursday is task editing and is gated on
`tasks.edit`; dragging a milestone is `milestones.edit`; a project deadline is
`projects.edit`. Each source declares which module owns it (`sources/base.py`)
and the drag endpoint asks that module, not this one. Otherwise the calendar
would become a way to edit half the system with one permission — the exact hole
the resource planner closed by routing its drag-and-drop through `tasks.assign`.
"""
from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect

from accounts.permissions import has_perm

MODULE = "calendar"


def can_view(user):
    return has_perm(user, MODULE, "view")


def can_create(user):
    return has_perm(user, MODULE, "create")


def can_edit(user):
    return has_perm(user, MODULE, "edit")


def can_delete(user):
    return has_perm(user, MODULE, "delete")


def can_manage(user, event):
    """Whether this user may change *this* event.

    `calendar.edit` is necessary but the organiser is always allowed: someone
    who scheduled their own one-to-one should not need a permission to move it,
    and a role edited at lunchtime shouldn't strand a meeting they own.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if can_edit(user):
        return True
    return event.created_by_id == user.pk


def calendar_required(view):
    """Redirect-with-message on denial, matching the app's established UX — the
    sidebar link is already hidden from anyone without access, so a denial means
    a stale tab or a hand-typed URL rather than a user who needs a 403."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not can_view(request.user):
            messages.error(request, "You don't have access to the calendar.")
            return redirect("core:dashboard")
        return view(request, *args, **kwargs)
    return wrapper


def create_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not can_view(request.user):
            messages.error(request, "You don't have access to the calendar.")
            return redirect("core:dashboard")
        if not can_create(request.user):
            messages.error(request, "You don't have permission to add events.")
            return redirect("calendar_hub:month")
        return view(request, *args, **kwargs)
    return wrapper
