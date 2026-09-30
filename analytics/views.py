"""Analytics screens.

Five read-only pages plus an export endpoint. Views stay thin on purpose: parse
the filters, ask `services` for numbers, ask `charts` for configs, render. All
the arithmetic is testable without a request, and all the SQL is in `selectors`.

Every page is gated by `analytics.view`; money is gated a second time by
`finance.view`, so a Project Manager gets the delivery picture with the revenue
columns simply absent — the same split the project page already makes.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from core.models import log_activity

from . import charts, exports, selectors, services
from .permissions import (analytics_required, can_export_analytics,
                          can_view_finance, export_required)


def _base_context(request):
    """Shared by every page: the parsed filters, the dropdown options and the
    two permission flags the templates branch on."""
    filters = selectors.Filters.from_request(request)
    finance_visible = can_view_finance(request.user)
    return {
        "filters": filters,
        "options": selectors.filter_options(request.user),
        "can_view_finance": finance_visible,
        "can_export": can_export_analytics(request.user),
        "querystring": filters.querystring(),
    }, filters, finance_visible


@login_required
@analytics_required
def dashboard(request):
    """Top cards + the eight charts."""
    context, filters, finance_visible = _base_context(request)

    hours = selectors.hours_breakdown(filters)
    task_counts = selectors.task_status_counts(filters)
    project_rows = services.project_metrics(filters,
                                            include_finance=finance_visible)
    employee_rows = services.employee_metrics(filters)
    department_rows = services.department_metrics(filters)

    context.update({
        # hours/task_counts are handed over rather than recomputed — the charts
        # below need the same two aggregates.
        "cards": services.overview_cards(filters, include_finance=finance_visible,
                                         hours=hours, task_counts=task_counts),
        "hours": hours,
        "task_counts": task_counts,
        "charts": charts.build_all(
            hours=hours,
            task_counts=task_counts,
            hour_series=selectors.daily_hours_series(filters),
            revenue_series=(selectors.monthly_revenue_series(filters)
                            if finance_visible else []),
            growth_series=(selectors.monthly_growth_series(filters)
                           if finance_visible else []),
            projects=project_rows,
            employees=employee_rows,
            departments=department_rows,
            include_finance=finance_visible,
            # So a drill-through keeps the range and filters already on screen.
            querystring=context["querystring"],
        ),
        # The tables under each chart are the accessible path to the same
        # numbers — a canvas is invisible to a screen reader — and they are
        # sliced to the chart's own limit rather than a round number, because
        # a bar with no matching row is a destination you can only reach by
        # clicking it.
        "top_projects": project_rows[:charts.DASHBOARD_HEALTH_ROWS],
        "top_employees": employee_rows[:charts.DASHBOARD_UTILIZATION_ROWS],
        "departments": department_rows,
        "active": "dashboard",
    })
    return render(request, "analytics/dashboard.html", context)


@login_required
@analytics_required
def employees(request):
    context, filters, finance_visible = _base_context(request)
    rows = services.employee_metrics(filters)
    context.update({
        "rows": rows,
        "trend": services.productivity_trend(filters),
        "charts": {
            "employee-utilization": charts.employee_utilization(
                rows, limit=25, querystring=context["querystring"]),
            "hours-trend": charts.hours_trend(
                selectors.daily_hours_series(filters)),
        },
        "dataset": "employees",
        "active": "employees",
    })
    return render(request, "analytics/employees.html", context)


@login_required
@analytics_required
def projects(request):
    context, filters, finance_visible = _base_context(request)
    rows = services.project_metrics(filters, include_finance=finance_visible)
    context.update({
        "rows": rows,
        "charts": {"project-health": charts.project_health(rows, limit=20)},
        "dataset": "projects",
        "active": "projects",
    })
    return render(request, "analytics/projects.html", context)


@login_required
@analytics_required
def finance(request):
    """Finance analytics is the one page that is entirely money, so it is
    gated wholesale rather than column-by-column."""
    context, filters, finance_visible = _base_context(request)
    if not finance_visible:
        return render(request, "analytics/no_finance.html", context, status=403)

    context.update({
        "metrics": services.finance_metrics(filters),
        "charts": {
            "revenue-trend": charts.revenue_trend(
                selectors.monthly_revenue_series(filters)),
            "monthly-growth": charts.monthly_growth(
                selectors.monthly_growth_series(filters)),
        },
        "active": "finance",
    })
    return render(request, "analytics/finance.html", context)


@login_required
@analytics_required
def clients(request):
    context, filters, finance_visible = _base_context(request)
    context.update({
        "rows": services.client_metrics(filters, include_finance=finance_visible),
        "dataset": "clients",
        "active": "clients",
    })
    return render(request, "analytics/clients.html", context)


@login_required
@export_required
def export(request, dataset, fmt):
    """CSV/XLSX of whichever table the user is looking at, with the same
    filters applied — a download that doesn't match the screen is a support
    ticket waiting to happen.

    `dataset` and `fmt` are validated against fixed maps rather than used to
    look anything up dynamically; a URL is not a place to accept a function
    name from.
    """
    if dataset not in exports.DATASETS or fmt not in ("csv", "xlsx"):
        # Unreachable through the UI — the URL converters already constrain
        # both — so this is the hand-typed-URL path.
        return render(request, "analytics/no_finance.html",
                      {"unknown_export": True}, status=404)

    filters = selectors.Filters.from_request(request)
    finance_visible = can_view_finance(request.user)
    label, build = exports.DATASETS[dataset]

    rows = {
        "employees": lambda: services.employee_metrics(filters),
        "projects": lambda: services.project_metrics(
            filters, include_finance=finance_visible),
        "clients": lambda: services.client_metrics(
            filters, include_finance=finance_visible),
        "departments": lambda: services.department_metrics(filters),
    }[dataset]()

    headers, data = build(rows, include_finance=finance_visible)
    log_activity(request.user, "exported analytics",
                 f"{label} · {filters.start} to {filters.end} · {fmt.upper()}")

    if fmt == "csv":
        return exports.csv_response(dataset, headers, data)
    return exports.xlsx_response(dataset, label, headers, data)
