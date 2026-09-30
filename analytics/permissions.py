"""Access rules for the analytics surface.

Nothing new is invented here — this is a thin layer over
`accounts.permissions.has_perm`, so the RBAC matrix at /settings/roles/ stays
the single place permissions are decided. The `analytics` module key is already
in the catalog and already seeded: Super Admin and Manager hold every action;
Project Manager and Accounts hold `view` alone (granted by migration 0012, so
they read the dashboards without being able to export them). Developer,
Designer, Sales, HR and Client hold nothing here — analytics reports across
people, which is what per-assignee task visibility exists to limit.

Two gates:

* `analytics.view`   — open the dashboards.
* `analytics.export` — download the CSV/XLSX. Kept separate on purpose: a
  dashboard is a glance, an export is a file that leaves the building with
  salary-adjacent productivity data in it.

Finance figures carry a third, orthogonal gate: `finance.view`, the same one
that governs money everywhere else in the app (dashboard, project detail,
invoices). A Project Manager can read delivery analytics without revenue
appearing, exactly as they can on the project page.
"""
from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect

from accounts.permissions import has_perm

MODULE = "analytics"


def can_view_analytics(user):
    return has_perm(user, MODULE, "view")


def can_export_analytics(user):
    return has_perm(user, MODULE, "export")


def can_view_finance(user):
    """Finance figures are gated by the finance module, not by analytics.

    Without this, granting someone `analytics.view` would hand them revenue and
    outstanding balances through the back door — the one thing the finance
    gating everywhere else exists to prevent.
    """
    return has_perm(user, "finance", "view")


def analytics_required(view):
    """Matches the app's established denial UX: redirect with a message rather
    than a 403, because the sidebar link is already hidden for anyone who
    can't be here — a denial means a stale tab or a hand-typed URL."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not can_view_analytics(request.user):
            messages.error(request, "You don't have access to analytics.")
            return redirect("core:dashboard")
        return view(request, *args, **kwargs)
    return wrapper


def export_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not can_view_analytics(request.user):
            messages.error(request, "You don't have access to analytics.")
            return redirect("core:dashboard")
        if not can_export_analytics(request.user):
            messages.error(request, "You don't have permission to export analytics.")
            return redirect("analytics:dashboard")
        return view(request, *args, **kwargs)
    return wrapper
