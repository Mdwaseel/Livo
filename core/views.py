from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import redirect, render

from django.db.models import Count

from accounts.permissions import has_perm, is_superadmin, require_perm
from analytics import charts
from clients.models import Client
from projects.access import visible_projects, visible_tasks
from projects.models import Project, Task
from documents.access import visible_documents
from documents.models import Document

from . import views_executive
from .models import ActivityLog, AgencySettings
from .tenancy import home_user_required, scope
from .uploads import process_upload


@login_required
def dashboard(request):
    """The front page, which is two different pages.

    Anyone holding `analytics.view` gets the executive rollup: the whole
    business on one screen, which is what somebody running the company opens a
    laptop to see. Everyone else gets the personal dashboard below — their
    projects, their documents, their recent activity — because a delivery
    engineer does not need the agency's health score and, per migration 0009,
    is not allowed cross-people reporting anyway.

    One URL, not two. A company gets one front page, and a leadership view
    nobody lands on by default is a report rather than a dashboard.
    """
    if views_executive.can_see_executive(request.user):
        return views_executive.render_dashboard(request)
    return _personal_dashboard(request)


def _personal_dashboard(request):
    # Every figure below is scoped to the projects this viewer can see, so the
    # dashboard describes their portfolio rather than the agency's.
    live = visible_projects(
        Project.objects.filter(is_archived=False), request.user)
    live_ids = list(live.values_list("pk", flat=True))
    # No money is computed here any more. Revenue, outstanding and the
    # collection position moved to the Business overview (core.views_business)
    # behind the `business` module, so this page is safe to have on screen in
    # front of anyone — the three aggregate queries that produced those figures
    # went with them rather than being computed and then not rendered.
    #
    # Counts run over the same visibility filter as the list below them: a user
    # who can't see invoices shouldn't have them counted into their totals.
    # `project__pk__in` matters as much as the type filter — without it the
    # "Recent documents" card happily lists a contract from a project the
    # viewer is barred from opening.
    live_docs = visible_documents(
        Document.objects.filter(is_archived=False, project__pk__in=live_ids),
        request.user)
    ctx = {
        "client_count": Client.objects.filter(
            is_archived=False, projects__pk__in=live_ids).distinct().count(),
        "project_count": live.count(),
        "document_count": live_docs.count(),
        "pending_count": live_docs.filter(status=Document.Status.REVIEW).count(),
        "recent_documents": live_docs.select_related(
            "project", "project__client", "document_type")[:8],
        "recent_projects": visible_projects(
            Project.objects.select_related("client"), request.user)[:5],
        # The feed is workspace-scoped like everything above it: an entry
        # reading "created project Acme Rebrand" names a project the viewer may
        # not be allowed to know exists.
        "recent_activity": scope(
            ActivityLog.objects.select_related("user"), request.user)[:8],
        "charts": _dashboard_charts(request.user, live, live_docs),
    }
    return render(request, "core/dashboard.html", ctx)


def _counts_by(queryset, field, choices):
    """(label, count) for every choice, in the order the choices declare.

    Every stage is listed even at zero, because the gaps are the information:
    a pipeline with nothing in Proposal Sent is a fact about the business, and
    dropping the empty bar would quietly hide it. One GROUP BY, not one query
    per stage.
    """
    tally = {row[field]: row["n"] for row
             in queryset.values(field).annotate(n=Count("id"))}
    return [(label, tally.get(value, 0)) for value, label in choices]


def _dashboard_charts(user, projects, documents):
    """The three charts on the home dashboard.

    None of them touches money — that is the whole arrangement: the figures
    that cannot be shown to a room live behind `business.view`, and these are
    the ones that can. They also answer the questions the dashboard is actually
    opened for: where the work is, what is on the board, what is waiting on
    somebody.

    They do not drill through on click, unlike the analytics charts. A clickable
    mark has to lead somewhere real, and neither the project list nor the task
    board takes a status filter to land on — a click that dumps you on an
    unfiltered list is worse than no click at all.
    """
    tasks = visible_tasks(
        Task.objects.filter(project__in=projects), user)
    return {
        "project-pipeline": charts.project_pipeline(
            _counts_by(projects, "status", Project.Status.choices)),
        "task-board": charts.task_board(
            _counts_by(tasks, "status", Task.Status.choices)),
        "document-pipeline": charts.document_pipeline([
            (label, count, color) for (label, count), color in zip(
                _counts_by(documents, "status", Document.Status.choices),
                charts.DOCUMENT_STATE_COLORS)
        ]),
    }


@login_required
def activity(request):
    """The full audit trail, paginated.

    Gated on the audit_logs module rather than a hardcoded super-admin check,
    so the RBAC matrix actually governs it (the module was in the catalog but
    nothing read it). Super admins pass either way.
    """
    if not (is_superadmin(request.user)
            or has_perm(request.user, "audit_logs", "view")):
        messages.error(request, "You don't have access to the activity log.")
        return redirect("core:dashboard")
    entries = scope(ActivityLog.objects.select_related("user"), request.user)
    page = Paginator(entries, 50).get_page(request.GET.get("page"))
    return render(request, "core/activity.html", {"page": page})


@login_required
def notifications(request):
    """List the user's notifications; opening the page marks them read."""
    items = list(request.user.notifications.all()[:100])
    request.user.notifications.filter(is_read=False).update(is_read=True)
    return render(request, "core/notifications.html", {"items": items})


@login_required
@home_user_required
@require_perm("agency_settings", "edit")
def agency_settings(request):
    """M7: owner-only branding / agency details screen.

    Home staff only, on top of the RBAC action. Branding is deliberately
    shared by every workspace — a partner's invoices carry our letterhead — so
    this one form writes the agency name, logo, GSTIN and place of supply that
    every workspace's documents are generated with. Editing it is not something
    a partner super admin gets to do from inside their own partition.
    """
    agency = AgencySettings.load()
    if request.method == "POST":
        for field in ("agency_name", "tagline", "primary_color", "email",
                      "phone", "address", "state", "gstin", "website"):
            setattr(agency, field, request.POST.get(field, getattr(agency, field)))
        days = (request.POST.get("default_annual_leave_days") or "").strip()
        if days.isdigit():
            agency.default_annual_leave_days = int(days)
        if request.FILES.get("logo"):
            try:
                agency.logo = process_upload(request.FILES["logo"])
            except ValidationError as exc:
                messages.error(request, exc.messages[0])
                return redirect("core:settings")
        agency.save()
        messages.success(request, "Agency settings saved.")
        return redirect("core:settings")
    return render(request, "core/settings.html", {"a": agency})
