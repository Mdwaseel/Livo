"""The Business overview — the confidential money screen.

Everything on this page used to sit on the dashboard, where anyone who happened
to hold `finance.view` saw the agency's cash position at a glance, and anyone
standing behind them saw it too. It now lives here, behind its own RBAC module.

**Why a new module rather than `finance.view`.** Plenty of roles hold
`finance.view` because they need to open an invoice — Accounts, Sales, a PM
quoting for work. None of them need the agency's revenue, outstanding balance
and collection rate. Reusing that key would have moved the page without
narrowing the audience, which is the entire point. `business.view` is seeded to
Super Admin alone; handing it to somebody is a deliberate tick in
/settings/roles/.

**Workspace-scoped like everything else.** The numbers come from
`analytics.selectors`, which narrows by the viewer's workspace — so a partner
running their own world sees their own figures here and never ours.

**The privacy toggle** is the second half of the answer. Being able to open the
page is one thing; having it on screen while you share a call is another. The
figures render masked and are revealed by a deliberate click, with the choice
remembered per device.
"""
from datetime import date, timedelta
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone

from accounts.permissions import has_perm
from analytics import charts, selectors, services

MODULE = "business"
ZERO = Decimal("0")


def business_required(view):
    """Redirect-with-a-message, matching the app's established denial UX.

    The sidebar link is hidden for anyone without the module, so a denial here
    is a stale tab or a hand-typed URL rather than someone being surprised.
    """
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not has_perm(request.user, MODULE, "view"):
            messages.error(request, "You don't have access to the business overview.")
            return redirect("core:dashboard")
        return view(request, *args, **kwargs)
    return wrapper


def _range(request):
    """The window this page reports on. Defaults to the last 12 months.

    A longer default than the analytics dashboard's 90 days on purpose: this
    page answers "how is the business doing", and a quarter is too short a
    window to see a trend in.
    """
    today = timezone.localdate()
    start = _parse_date(request.GET.get("start")) or (today - timedelta(days=365))
    end = _parse_date(request.GET.get("end")) or today
    if start > end:
        start, end = end, start
    return start, end


def _parse_date(raw):
    try:
        return date.fromisoformat((raw or "").strip())
    except (ValueError, AttributeError):
        return None


@login_required
@business_required
def overview(request):
    start, end = _range(request)
    filters = selectors.Filters(start=start, end=end, viewer=request.user)

    finance = services.finance_metrics(filters)
    revenue_series = selectors.monthly_revenue_series(filters)
    growth_series = selectors.monthly_growth_series(filters)
    client_rows = services.client_metrics(filters, include_finance=True)

    # Who owes the most, largest first — the working list, not a ranking for
    # its own sake.
    owing = sorted(
        (row for row in client_rows if (row.get("outstanding") or ZERO) > ZERO),
        key=lambda row: row["outstanding"], reverse=True)[:10]

    built = {
        "revenue-trend": charts.revenue_trend(revenue_series),
        "monthly-growth": charts.monthly_growth(growth_series),
        "collection": charts.collection_gauge(finance),
    }
    # The by-client chart is drawn from `owing` and nothing else. It used to
    # fall back to every client when nothing was outstanding, which drew ten
    # bars that opened a client on click above a card reading "Nothing
    # outstanding" and listing no rows at all — clickable marks with no link
    # beside them, which is the one thing these charts are not allowed to be.
    # With nothing owed there is no billed-against-collected gap to compare, so
    # the chart is omitted rather than filled with a different question.
    if owing:
        built["client-revenue"] = charts.client_revenue(owing)

    return render(request, "core/business.html", {
        "start": start,
        "end": end,
        "finance": finance,
        "owing": owing,
        "charts": built,
    })
