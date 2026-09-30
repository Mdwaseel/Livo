"""Template helpers for the analytics pages.

The URL helpers here exist for a specific reason. Several charts on
these pages drill through on click, and every one of them is paired with a
table carrying the same links — that table is the keyboard and screen-reader
path to the destination, so the two must agree exactly.

They agree because they are the same function. `charts_module.employee_url` and
`charts_module.department_url` build the chart's `onClick` targets; `filter_url`
below calls straight through to them for the table. Hand-writing
`?{{ querystring }}&employee={{ pk }}` in the template looked equivalent and
was not: appending rather than replacing leaves a stale `employee=` in the
string, so the URL grew a duplicate on every click and only worked because
Django happens to read the last one.
"""
from django import template

from analytics import charts as charts_module

register = template.Library()


@register.simple_tag
def employee_filter_url(user, querystring=""):
    """Where the employee-utilisation chart sends a click on this person."""
    return charts_module.employee_url(user, querystring)


@register.simple_tag
def department_filter_url(department, querystring=""):
    """Where the department charts send a click on this department."""
    return charts_module.department_url(department, querystring)


@register.filter
def chart_has_data(charts, chart_id):
    """`{% if charts|chart_has_data:chart_id %}` — used by the chart card.

    A filter rather than something the views compute, because every page that
    renders a chart includes the same partial: doing it here fixes the analytics
    dashboard, the employees page, the projects page and the business overview
    in one move, and cannot be forgotten by the next page that includes it.
    """
    return charts is not None and charts_module.has_data(charts.get(chart_id))
