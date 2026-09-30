"""The executive rollup that IS the dashboard.

**Where it lives.** This is what `/` renders for anyone holding
`analytics.view`. It is not a second dashboard parked on its own URL — a
company gets one front page, and a leadership view nobody lands on by default
is a report, not a dashboard. `core.views.dashboard` picks between this and the
lighter personal dashboard on that one permission; `/executive/` survives only
as a redirect so old links and bookmarks still arrive somewhere.

**Gating.** `analytics.view` decides which dashboard you get. The money on it
is gated by `business.view` — NOT by `finance.view`, and the difference is the
whole point. `core.views_business` exists because plenty of roles hold
`finance.view` in order to open an invoice (Accounts, Sales, a PM quoting for
work) and none of them need the agency's revenue, outstanding balance and
collection rate. Putting the same figures on the front page behind the weaker
key would have handed that audience the thing the split was created to keep
from them.

So: revenue, pipeline value, outstanding and the money charts appear here for
exactly the people who can already open /business/, which is Super Admin unless
somebody deliberately ticks the box. Everyone else does not get blanked-out
tiles — they get a page with no money on it at all, and a health score
renormalised without the commercial term, since a masked figure still tells you
it exists.

**Why the AI brief is fetched separately.** Groq calls take seconds and
sometimes tens of seconds. Putting one in this view would mean the front page
of the company renders at the speed of a third party's worst morning — the
exact failure this page exists to detect elsewhere. So it renders from the
database alone and the brief arrives afterwards over `brief_json`, cached, its
absence a quiet line rather than an error. The arithmetic brief beside it is
always there and asks nobody's permission.
"""
from datetime import date, timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from accounts.permissions import has_perm
from analytics import charts, executive, selectors, services

from .tenancy import current_scope

MODULE = "analytics"
MONEY_MODULE = "business"

# The window the page opens on. Six months: long enough for a trend to be a
# trend, short enough that "this period" still means something an executive
# recognises as recent.
DEFAULT_WINDOW_DAYS = 182

# How long a generated AI brief is reused. An hour: the figures under it move
# over a working day, not over a minute, and regenerating per page load would
# spend a rate limit telling somebody the same thing twice.
BRIEF_TTL_SECONDS = 3600


def can_see_executive(user):
    return has_perm(user, MODULE, "view")


def can_see_money(user):
    """The agency's cash position, not "may this person open an invoice".

    Deliberately `business.view` rather than `finance.view` — see the module
    docstring. If this ever drifts back to the finance key, the front page
    starts showing revenue to every role that can read a quotation.
    """
    return has_perm(user, MONEY_MODULE, "view")


def executive_required(view):
    """Redirect-with-a-message, the app's established denial UX."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not can_see_executive(request.user):
            messages.error(request, "You don't have access to the executive dashboard.")
            return redirect("core:dashboard")
        return view(request, *args, **kwargs)
    return wrapper


def _range(request):
    today = timezone.localdate()
    start = _parse_date(request.GET.get("start")) or (
        today - timedelta(days=DEFAULT_WINDOW_DAYS))
    end = _parse_date(request.GET.get("end")) or today
    if start > end:
        start, end = end, start
    return start, end


def _parse_date(raw):
    try:
        return date.fromisoformat((raw or "").strip())
    except (ValueError, AttributeError):
        return None


def _rollup(request):
    """Everything the page and the brief both need, computed once.

    Pulled out because `render_dashboard` and `brief_json` must agree to the
    digit — a brief interpreting a different set of numbers from the ones on
    screen beside it is worse than no brief at all.
    """
    start, end = _range(request)
    filters = selectors.Filters(start=start, end=end, viewer=request.user)
    include_finance = can_see_money(request.user)

    hours = selectors.hours_breakdown(filters)
    task_counts = selectors.task_status_counts(filters)
    projects = services.project_metrics(filters, include_finance=include_finance)
    clients = services.client_metrics(filters, include_finance=include_finance)
    departments = services.department_metrics(filters)
    finance = services.finance_metrics(filters) if include_finance else {}

    health = executive.business_health(
        projects=projects, clients=clients, departments=departments,
        finance=finance, include_finance=include_finance)

    return {
        "filters": filters, "start": start, "end": end,
        "include_finance": include_finance,
        "hours": hours, "task_counts": task_counts,
        "projects": projects, "clients": clients,
        "departments": departments, "finance": finance, "health": health,
    }


def _previous_figures(filters, *, include_finance):
    """Just the four figures the change arrows compare against.

    Deliberately not a second `_rollup`: that would double every query on the
    page to put four percentages on screen. Only what the arrows actually need
    is recomputed, and the health score is the one costly item because it
    cannot be had without the metric rows it averages.
    """
    previous = executive.previous_window(filters)
    figures = {"client_count": selectors.client_count(previous)}

    projects = services.project_metrics(previous, include_finance=include_finance)
    clients = services.client_metrics(previous, include_finance=include_finance)
    departments = services.department_metrics(previous)
    finance = services.finance_metrics(previous) if include_finance else {}

    figures["health_score"] = executive.business_health(
        projects=projects, clients=clients, departments=departments,
        finance=finance, include_finance=include_finance)["score"]

    if include_finance:
        figures["revenue"] = finance["revenue"]
        figures["pipeline_value"] = selectors.open_pipeline_totals(
            previous)["value"]
    return figures


def render_dashboard(request):
    """The page itself. Called by `core.views.dashboard`, not routed directly."""
    data = _rollup(request)
    filters = data["filters"]
    include_finance = data["include_finance"]

    pipeline = selectors.pipeline_by_stage(filters)
    bands = executive.client_health_bands(data["clients"])
    scorecards = executive.department_scorecards(data["departments"])
    alerts = executive.alerts(
        filters, include_finance=include_finance, projects=data["projects"],
        clients=data["clients"], departments=data["departments"],
        hours=data["hours"], task_counts=data["task_counts"])

    built = {
        "task-flow": charts.task_flow(selectors.weekly_task_flow(filters)),
        "client-health": charts.client_health_bands(bands),
        "employee-health": charts.utilisation_trend(
            executive.utilisation_trend(filters, data["departments"])),
    }
    if scorecards:
        built["department-scorecard"] = charts.department_scorecard(scorecards)
    # Both money charts are omitted rather than emptied for a finance-blind
    # viewer: `has_data` would already hide an all-zero chart, but an axis
    # labelled in rupees is itself a disclosure about what is tracked here.
    if include_finance:
        built["revenue-trend"] = charts.revenue_trend(data["finance"]["trend"])
        built["pipeline-stages"] = charts.pipeline_stages(pipeline)

    return render(request, "core/executive.html", {
        "start": data["start"],
        "end": data["end"],
        "can_view_money": include_finance,
        "health": data["health"],
        "tiles": executive.headline(
            filters, include_finance=include_finance, finance=data["finance"],
            clients=data["clients"], health=data["health"],
            previous=_previous_figures(filters,
                                       include_finance=include_finance)),
        "stats": executive.secondary(
            projects=data["projects"], hours=data["hours"],
            task_counts=data["task_counts"]),
        "alerts": alerts,
        "actions": executive.recommended_actions(alerts),
        "brief": executive.management_brief(
            filters, include_finance=include_finance, projects=data["projects"],
            clients=data["clients"], hours=data["hours"],
            task_counts=data["task_counts"], finance=data["finance"]),
        "pipeline": pipeline,
        "bands": bands,
        "scorecards": scorecards,
        # Worst health first — this page is opened to find problems, so the
        # problems go where the eye lands. `project_metrics` already sorts that
        # way; the slice is here so the template does not decide how many.
        # Same two counts the personal dashboard publishes, so the
        # workspace-scoping contract can be asserted against whichever
        # dashboard a given viewer happens to get.
        "project_count": len(data["projects"]),
        "client_count": len(data["clients"]),
        "watchlist": data["projects"][:6],
        "top_clients": data["clients"][:6],
        "charts": built,
        "generated_at": timezone.localtime(),
        "record_count": _record_count(data),
        "active": "dashboard",
    })


def _record_count(data):
    """How many rows this page actually read, for the "analysed N records"
    line. Counted from the rollups already in hand rather than re-queried."""
    return (len(data["projects"]) + len(data["clients"])
            + len(data["departments"]) + data["task_counts"]["total"]
            + data["hours"].get("entries", 0)
            + (data["finance"].get("payment_count", 0)
               if data["include_finance"] else 0))


@login_required
def legacy_redirect(request):
    """`/executive/` was this page's own URL for one release. It is the
    dashboard now, so the old address forwards rather than 404s."""
    return redirect("core:dashboard")


@login_required
@executive_required
def brief_json(request):
    """The AI paragraph, fetched by the page after it has already rendered.

    Cached per (workspace, window, finance visibility). All three belong in the
    key: two workspaces are different companies, two windows are different
    questions, and a finance-blind viewer must never be handed a cached brief
    that mentions revenue because a Manager warmed the cache first.
    """
    data = _rollup(request)
    key = (f"exec-brief:{_scope_key(request.user)}:"
           f"{data['start']}:{data['end']}:{int(data['include_finance'])}")

    cached = cache.get(key)
    if cached is not None:
        # `False` is the remembered "we tried and got nothing", which stops a
        # refresh from re-attempting a call that just failed.
        return JsonResponse({"text": cached or None, "cached": True})

    from ai_engine.services import business_brief

    text = business_brief(_facts(data))
    cache.set(key, text or False, BRIEF_TTL_SECONDS)
    return JsonResponse({"text": text, "cached": False})


def _scope_key(user):
    """A stable cache-key fragment for the viewer's workspace scope.

    Built from the sorted ids rather than the Scope tuple's repr: `ids` is a
    frozenset, whose repr ordering is not guaranteed stable across processes,
    and an unstable key would quietly give each gunicorn worker its own entry.
    `all` is the home super admin's every-workspace position.
    """
    ids = current_scope(user).ids
    return "all" if ids is None else ",".join(str(i) for i in sorted(ids))


def _facts(data):
    """The figures handed to the model — a flat, named dict and nothing else.

    Deliberately not the full rollup: no querysets, no model instances, and no
    client or employee names. The model gets what it needs to interpret the
    numbers and no more, because everything in here leaves the building.
    """
    health = data["health"]
    hours = data["hours"]
    tasks = data["task_counts"]
    projects = data["projects"]

    facts = {
        "period": f"{data['start']} to {data['end']}",
        "business_health_score": health["score"],
        "health_components": {
            c["label"]: c["score"] for c in health["components"]
            if c["score"] is not None
        },
        "projects_total": len(projects),
        "projects_at_risk": sum(1 for r in projects
                                if r["risk"]["level"] == "HIGH"),
        "projects_past_target": sum(1 for r in projects if r["is_overdue"]),
        "clients_total": len(data["clients"]),
        "clients_without_live_work": sum(1 for r in data["clients"]
                                         if r["active_projects"] == 0),
        "tasks_completed": tasks["done"],
        "tasks_open": tasks["total"] - tasks["done"],
        "tasks_overdue": tasks["overdue"],
        "hours_logged": float(hours["total"]),
        "hours_billable": float(hours["billable"]),
        "worklogs_awaiting_approval": hours.get("pending_count", 0),
        "departments": {
            row["department"].name: {
                "headcount": row["headcount"],
                "utilisation_percent": row["utilization_percent"],
            } for row in data["departments"]
        },
    }
    if data["include_finance"]:
        finance = data["finance"]
        facts.update({
            "currency": "INR",
            "revenue_this_period": float(finance["revenue"]),
            "payments_received": finance["payment_count"],
            "portfolio_value": float(finance["portfolio_value"]),
            "outstanding": float(finance["outstanding"]),
            "collection_percent": finance["collection_percent"],
        })
    return facts
