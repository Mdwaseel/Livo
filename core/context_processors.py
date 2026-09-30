from accounts.permissions import (
    LEGACY_MAP, has_perm, is_admin_level, is_superadmin, user_can,
)

from .models import AgencySettings
from .tenancy import (
    ALL, SESSION_KEY, can_switch_workspace, is_home_user, switchable_workspaces,
)
from .uploads import max_upload_bytes, max_upload_label


def _pending_leave_count(user, approves_all):
    """How many leave requests are waiting on this person.

    Exactly one query, for everybody — the same per-page cost the unread
    notification bell above already pays, and for the same reason: a queue you
    are not told about is a queue nobody works.

    `approves_all` is passed in rather than recomputed because the caller has
    already asked; `has_perm` is cached per request, but the honest reading is
    that this function does ONE query and no permission work.

    Workspace-scoped like everything else, so a partner's manager is never
    shown a count that includes our team's requests.
    """
    from core.tenancy import scope

    from resource_planner.models import LeaveRecord

    waiting = scope(
        LeaveRecord.objects.filter(status=LeaveRecord.Status.REQUESTED),
        user, path="user__workspace",
    ).exclude(user=user)          # never your own
    if not approves_all:
        # Team leads without the blanket action still decide for their reports.
        waiting = waiting.filter(user__reports_to=user)
    return waiting.count()


def uploads(request):
    """The upload ceiling, so the size hint next to a file input and the check
    that enforces it both read the same number as the server does."""
    return {
        "max_upload_bytes": max_upload_bytes(),
        "max_upload_label": max_upload_label(),
    }


def branding(request):
    """Make agency branding available in every template."""
    try:
        return {"agency": AgencySettings.load()}
    except Exception:
        return {"agency": None}


def workspace(request):
    """Per-request capabilities + unread notification count for the chrome.

    `can.<legacy-key>` is kept for existing templates but is now computed from
    the DB-driven RBAC engine (see accounts.permissions.LEGACY_MAP). New
    templates should prefer {% user_can user 'module' 'action' %}."""
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {"can": {}, "is_superadmin": False, "is_admin_level": False,
                "can_view_finance": False, "unread_count": 0,
                "active_workspace": None, "can_switch_workspace": False,
                "workspace_choices": [], "workspace_is_all": False,
                "is_home_user": False, "is_home_admin": False}
    approves_leave = has_perm(user, "leaves", "approve")
    switching = can_switch_workspace(user)
    at_home = is_home_user(user)
    session = getattr(request, "session", None)
    return {
        "can": {module: user_can(user, module) for module in LEGACY_MAP},
        "is_superadmin": is_superadmin(user),
        # Super Admin or Manager — gates the whole Admin sidebar section.
        "is_admin_level": is_admin_level(user),
        # Single gate for every finance surface (money figures + invoices/
        # quotations/proposals). Toggled per role via Finance → view in the
        # RBAC matrix at /settings/roles/.
        "can_view_finance": has_perm(user, "finance", "view"),
        "unread_count": user.notifications.filter(is_read=False).count(),
        # Leave approvals appear in the sidebar for holders of `leaves.approve`
        # and for anyone who currently has something to decide — a team lead
        # without the blanket action still approves for their own reports. The
        # link surfaces when the first request arrives rather than sitting there
        # empty, which is what keeps this to a single query.
        "pending_leave_count": _pending_leave_count(user, approves_leave),
        # --- tenancy (see core.tenancy) ---
        # `active_workspace` is what the topbar names and what new records get
        # stamped with. It is None under the "All workspaces" position, which
        # is why the template needs `workspace_is_all` as a separate flag
        # rather than testing the object for falsiness.
        "active_workspace": getattr(request, "workspace", None),
        "workspace_is_all": (switching and session is not None
                             and session.get(SESSION_KEY) == ALL),
        "can_switch_workspace": switching,
        "workspace_choices": switchable_workspaces(user) if switching else [],
        "is_home_user": at_home,
        # Gates the workspace admin screens in the sidebar. Distinct from
        # `is_superadmin`: a partner is a super admin who must not see these.
        "is_home_admin": at_home and is_superadmin(user),
    }
