"""The executive rollup — one screen, every module, real numbers only.

`services.py` answers a module at a time: how are projects doing, how are
people doing, how is the money. This module asks the question none of them can
answer alone — *how is the business doing* — by blending those answers into a
single score, then explaining the score by naming the things that pulled it
down.

Three rules it is built on:

**Nothing is invented.** Every figure traces to a row somebody entered. Where
this install has no data for something an executive dashboard normally shows —
support tickets, SLA compliance, AMC renewals, stock levels, CSAT, engagement
surveys — there is no tile, no placeholder and no plausible-looking zero. A
dashboard that quietly makes a number up is worse than one that admits the gap,
because the made-up number is the one that gets repeated in a meeting.

**Every score is decomposable.** `business_health` returns its components with
their own scores and weights, not just a total, so the page can say *82, and
here is the 18 you lost and where*. A blended number nobody can take apart is a
number nobody can act on.

**The viewer's permissions decide the arithmetic, not just the rendering.**
Every entry point takes `include_finance` and threads it down. Someone without
`finance.view` gets a health score computed from delivery and people alone
rather than a score with a hidden money term they cannot see — matching the way
`services.project_health_score` already handles the same split.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from projects.models import Project

from . import selectors, services

ZERO = Decimal("0")

# Health bands. The cut-offs are the same three everywhere on the page — the
# score chip, the client doughnut, the department cards — so a "healthy" client
# and a "healthy" department mean the same thing.
BAND_HEALTHY = 70
BAND_MONITOR = 45

# Utilisation is scored against a band, not a target, because both directions
# are a problem: under this and the team is idle, over it and they are heading
# for burnout. The peak of the curve sits at UTILISATION_IDEAL.
UTILISATION_IDEAL = 80.0
UTILISATION_TOLERANCE = 25.0

# What counts as "a lot" when an alert has to pick a tone.
ALERT_OVERDUE_TASKS = 3
ALERT_PENDING_APPROVALS = 5


# ---------------------------------------------------------------------------
# the composite
# ---------------------------------------------------------------------------

def business_health(*, projects, clients, departments, finance,
                    include_finance=True):
    """{score, band, components[]} — the number and its receipts.

    Weights are declared here as data rather than baked into an expression so
    that dropping the money component for a finance-blind viewer is a filter
    followed by a renormalise, not a second copy of the formula. The remaining
    components then share the freed weight in their existing proportions, which
    is why a PM and a Manager can read different scores off the same data and
    both be right.
    """
    components = [
        _component("Delivery", _avg(row["health"] for row in projects), 35,
                   f"{len(projects)} live project"
                   f"{'' if len(projects) == 1 else 's'}"),
        _component("Customers", _avg(row["health"] for row in clients), 25,
                   f"{len(clients)} account{'' if len(clients) == 1 else 's'}"),
        _component("People", _utilisation_score(departments), 20,
                   _utilisation_note(departments)),
    ]
    if include_finance:
        collected = finance.get("collection_percent") or 0.0
        components.append(
            _component("Commercial", collected, 20,
                       f"{collected:.0f}% of portfolio collected"))

    # Components with nothing behind them are dropped rather than scored zero.
    # An install with no clients yet is not a business in poor health, it is a
    # business we cannot score on that axis, and a zero would say the first.
    scored = [c for c in components if c["score"] is not None]
    if not scored:
        return {"score": None, "band": "unknown", "components": components}

    total_weight = sum(c["weight"] for c in scored)
    score = sum(c["score"] * c["weight"] for c in scored) / total_weight
    for component in scored:
        # The share of the FINAL score this component is responsible for, which
        # is what the reader actually wants — "Delivery is 28 of your 82".
        component["contribution"] = round(
            component["score"] * component["weight"] / total_weight, 1)
    return {
        "score": int(round(score)),
        "band": band_for(score),
        "components": components,
    }


def _component(label, score, weight, note):
    return {
        "label": label,
        "score": None if score is None else round(float(score), 1),
        "weight": weight,
        "note": note,
        "band": "unknown" if score is None else band_for(score),
        "contribution": None,
    }


def band_for(score):
    if score is None:
        return "unknown"
    if score >= BAND_HEALTHY:
        return "healthy"
    if score >= BAND_MONITOR:
        return "monitor"
    return "risk"


def _avg(values):
    values = [float(v) for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _utilisation_score(departments):
    """Distance from the ideal band, in both directions, as 0-100.

    A department at 80% scores 100; one at 55% or 105% scores 0. Averaged
    across departments weighted by headcount, so a two-person team running hot
    does not outweigh a twenty-person team sitting comfortably.
    """
    people = sum(row["headcount"] for row in departments)
    if not people:
        return None
    total = 0.0
    for row in departments:
        distance = abs(row["utilization_percent"] - UTILISATION_IDEAL)
        score = max(0.0, 100.0 * (1 - distance / UTILISATION_TOLERANCE))
        total += score * row["headcount"]
    return total / people


def _utilisation_note(departments):
    people = sum(row["headcount"] for row in departments)
    if not people:
        return "No departments with headcount"
    hours = sum(float(row["hours"]) for row in departments)
    weighted = sum(row["utilization_percent"] * row["headcount"]
                   for row in departments) / people
    return (f"{weighted:.0f}% utilisation across {people} "
            f"{'person' if people == 1 else 'people'} - {hours:,.0f} h logged")


# ---------------------------------------------------------------------------
# headline figures
# ---------------------------------------------------------------------------

def previous_window(filters):
    """The window of equal length immediately before this one.

    Equal length, not "last month": comparing a 6-month window against a
    calendar month would put a percentage on screen that means nothing, and a
    percentage that means nothing is the one an executive quotes.
    """
    from dataclasses import replace

    span = filters.end - filters.start
    end = filters.start - timedelta(days=1)
    return replace(filters, start=end - span, end=end)


def delta(current, previous):
    """{percent, direction} or None when there is nothing to compare against.

    None rather than 0% or "+100%" when the previous period is empty: a
    business that billed nothing last month and something this month has not
    grown by a percentage, it has started, and dressing that up as +100% is a
    number somebody will repeat.
    """
    current, previous = float(current or 0), float(previous or 0)
    if not previous:
        return None
    change = (current - previous) / abs(previous) * 100
    return {
        "percent": abs(round(change, 1)),
        "direction": "up" if change > 0 else "down" if change < 0 else "flat",
    }


def headline(filters, *, include_finance, finance, clients, health,
             previous=None):
    """The big tiles. Money tiles are omitted entirely without `finance.view`
    rather than rendered blank — an empty box on a dashboard reads as broken,
    an absent one reads as not-for-you, and the sidebar already made that
    distinction elsewhere in this app.

    `previous` is the same handful of figures for the preceding window of equal
    length; it supplies the change arrow on each tile. Passing it is optional,
    so a caller that cannot afford the second pass gets tiles without arrows
    rather than tiles with arrows pointing at nothing.
    """
    previous = previous or {}
    tiles = []
    if include_finance:
        pipeline = selectors.open_pipeline_totals(filters)
        count = pipeline["count"]
        tiles.append({
            "label": "Total revenue",
            "value": finance["revenue"],
            "kind": "money",
            "note": (f"{finance['payment_count']} payment"
                     f"{'' if finance['payment_count'] == 1 else 's'} "
                     f"in this period"),
            "band": None,
            "delta": delta(finance["revenue"], previous.get("revenue")),
        })
        tiles.append({
            "label": "Open pipeline",
            "value": pipeline["value"] or ZERO,
            "kind": "money",
            "note": (f"{count} project{'' if count == 1 else 's'} "
                     f"not yet closed"),
            "band": None,
            "delta": delta(pipeline["value"], previous.get("pipeline_value")),
        })
    bands = client_health_bands(clients)
    tiles.append({
        "label": "Active customers",
        "value": len(clients),
        "kind": "count",
        "note": (f"{bands['healthy']} healthy - {bands['monitor']} monitor - "
                 f"{bands['risk']} at risk") if clients else "No clients yet",
        "band": None,
        "delta": delta(len(clients), previous.get("client_count")),
    })
    tiles.append({
        "label": "Business health",
        "value": health["score"],
        "kind": "score",
        "note": _health_note(health),
        "band": health["band"],
        "delta": delta(health["score"], previous.get("health_score")),
    })
    return tiles


# ---------------------------------------------------------------------------
# recommended actions
# ---------------------------------------------------------------------------

# What to actually DO about each kind of alert, keyed on the alert title so the
# wording lives beside the finding it answers. None of them promises an
# outcome: the projected effect of an action ("recovers SLA to 96.4%") is
# exactly the kind of figure this page refuses to invent.
ACTION_FOR = {
    "Projects at risk": "Open a recovery review on the flagged projects and get "
                        "a written commitment against each slipped milestone.",
    "Overdue tasks": "Work the overdue list down: reassign what is blocked and "
                     "re-date what is genuinely no longer due.",
    "Work logs awaiting approval": "Clear the approval queue so those hours "
                                   "become billable.",
    "Workload above threshold": "Rebalance open work away from the departments "
                                "running hottest.",
    "Accounts with no live work": "Start a renewal conversation with the dormant "
                                  "accounts before the relationship goes cold.",
    "Delivery ahead of collection": "Raise invoices against the delivered "
                                    "milestones on these projects.",
}


def recommended_actions(alerts, *, limit=3):
    """The most severe alerts turned into numbered things to do.

    Derived from the alerts rather than computed separately, so an action can
    never appear for a risk the page is not also showing — the two halves
    cannot drift apart. `healthy` is filtered out: "nothing needs attention" is
    a finding, not a task.
    """
    actionable = [a for a in alerts if a["level"] != "healthy"]
    return [
        {
            "index": i,
            "title": alert["title"],
            "body": ACTION_FOR.get(alert["title"], alert["body"]),
            "metric": alert["metric"],
            "route": alert["route"],
            "level": alert["level"],
        }
        for i, alert in enumerate(actionable[:limit], start=1)
    ]


# ---------------------------------------------------------------------------
# people trend
# ---------------------------------------------------------------------------

def utilisation_trend(filters, departments):
    """[(month, utilisation%, billable%)] — the people-side line.

    Capacity is recomputed per month from headcount x working days x a standard
    day, so a short month does not read as a productivity dip. Headcount is
    taken as it stands today rather than reconstructed historically: the
    employee record carries no membership history to rebuild it from, and
    inventing one would put a made-up denominator under a real numerator.
    """
    people = sum(row["headcount"] for row in departments)
    series = []
    for month, total, billable in selectors.monthly_hours_series(filters):
        # Clamped to the window: the first and last months are usually partial,
        # and giving them a full month of capacity understates both.
        days = services.working_days(max(month, filters.start),
                                     min(_month_end(month), filters.end))
        capacity = Decimal(days) * services.STANDARD_DAY_HOURS * Decimal(people)
        series.append((month,
                       services._percent(total, capacity),
                       services._percent(billable, total)))
    return series


def _month_end(month):
    following = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return following - timedelta(days=1)


def _health_note(health):
    scored = [c for c in health["components"] if c["score"] is not None]
    if not scored:
        return "Not enough data to score yet"
    weakest = min(scored, key=lambda c: c["score"])
    if weakest["score"] >= BAND_HEALTHY:
        return "All tracked areas are healthy"
    return f"{weakest['label']} is pulling the score down"


def secondary(*, projects, hours, task_counts):
    """The thin strip under the headline: the operational numbers that do not
    warrant a big tile but are the first thing anyone asks about."""
    at_risk = [row for row in projects if row["risk"]["level"] == "HIGH"]
    overdue_projects = [row for row in projects if row["is_overdue"]]
    active = sum(1 for row in projects
                 if row["project"].status == Project.Status.ACTIVE)
    pending = hours.get("pending_count", 0)
    open_tasks = task_counts["total"] - task_counts["done"]
    billable_pct = services._percent(hours["billable"], hours["total"])

    return [
        {"label": "Active projects", "value": active,
         "note": (f"{len(at_risk)} at risk - {len(overdue_projects)} past target"
                  if at_risk or overdue_projects else "None flagged"),
         "tone": "warn" if at_risk else None},
        {"label": "Open tasks", "value": open_tasks,
         "note": (f"{task_counts['overdue']} overdue"
                  if task_counts["overdue"] else "None overdue"),
         "tone": "warn" if task_counts["overdue"] else None},
        {"label": "Billable hours", "value": f"{hours['billable']:,}",
         "note": f"{billable_pct:.0f}% of {hours['total']:,} logged",
         "tone": None},
        {"label": "Pending approvals", "value": pending,
         "note": "Work logs awaiting review" if pending else "Nothing queued",
         "tone": "warn" if pending >= ALERT_PENDING_APPROVALS else None},
    ]


def client_health_bands(clients):
    """{healthy, monitor, risk} counts from the same score the client table
    shows, so the doughnut and the rows can never disagree."""
    bands = {"healthy": 0, "monitor": 0, "risk": 0}
    for row in clients:
        bands[band_for(row["health"])] += 1
    return bands


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------

def alerts(filters, *, include_finance, projects, clients, departments,
           hours, task_counts):
    """Risks worth a manager's attention, most severe first.

    Every alert carries the module it came from and the figure that triggered
    it, because an alert you cannot verify is one you learn to ignore. Nothing
    here is a threshold invented for a demo — each one reads a number that is
    already on another screen in this app, and links to that screen.
    """
    found = []

    at_risk = [row for row in projects if row["risk"]["level"] == "HIGH"]
    if at_risk:
        worst = at_risk[0]
        found.append(_alert(
            "critical" if len(at_risk) > 1 else "warning",
            "Projects at risk",
            f"{len(at_risk)} project{'' if len(at_risk) == 1 else 's'} carry two "
            f"or more risk signals. Worst is {worst['project'].name}: "
            f"{', '.join(worst['risk']['reasons'])}.",
            module="Projects",
            metric=f"At risk: {len(at_risk)} of {len(projects)}",
            route="analytics:projects"))

    overdue = task_counts["overdue"]
    if overdue:
        found.append(_alert(
            "critical" if overdue >= ALERT_OVERDUE_TASKS else "warning",
            "Overdue tasks",
            f"{overdue} open task{'' if overdue == 1 else 's'} are past their due "
            f"date. Every one of them is somebody's commitment that has already "
            f"slipped.",
            module="Tasks",
            metric=f"Overdue: {overdue}",
            route="tasks:mine"))

    pending = hours.get("pending_count", 0)
    if pending:
        found.append(_alert(
            "warning" if pending >= ALERT_PENDING_APPROVALS else "monitor",
            "Work logs awaiting approval",
            f"{pending} entr{'y is' if pending == 1 else 'ies are'} queued for "
            f"review. Unapproved hours stay invisible to billing until somebody "
            f"signs them off.",
            module="Work logs",
            metric=f"Queued: {pending}",
            route="worklogs:mine"))

    ceiling = UTILISATION_IDEAL + UTILISATION_TOLERANCE
    hot = [row for row in departments
           if row["utilization_percent"] > ceiling]
    if hot:
        names = ", ".join(row["department"].name for row in hot)
        found.append(_alert(
            "warning", "Workload above threshold",
            f"{names} {'is' if len(hot) == 1 else 'are'} running above "
            f"{ceiling:.0f}% utilisation for this period. Sustained overload "
            f"shows up as attrition before it shows up in delivery.",
            module="Employees",
            metric=f"Above threshold: {len(hot)} department"
                   f"{'' if len(hot) == 1 else 's'}",
            route="analytics:employees"))

    idle = [row for row in clients if row["active_projects"] == 0]
    if idle:
        found.append(_alert(
            "monitor", "Accounts with no live work",
            f"{len(idle)} client{'' if len(idle) == 1 else 's'} have no active "
            f"project. That is a renewal conversation, and it is the thing a "
            f"revenue figure on its own hides.",
            module="Customers",
            metric=f"Dormant: {len(idle)} of {len(clients)}",
            route="analytics:clients"))

    if include_finance:
        exposed = [row for row in projects
                   if (row.get("outstanding") or ZERO) > ZERO
                   and row["completion_percent"] > 60
                   and row.get("collection_percent", 0) < 25]
        if exposed:
            total = sum(row["outstanding"] for row in exposed)
            found.append(_alert(
                "critical", "Delivery ahead of collection",
                f"{len(exposed)} project{'' if len(exposed) == 1 else 's'} are "
                f"more than 60% delivered with under a quarter of the budget "
                f"collected. That is work already done and not yet paid for.",
                module="Finance",
                metric=f"Exposed: {total:,.0f}",
                route="core:business"))

    if not found:
        found.append(_alert(
            "healthy", "Nothing needs attention",
            "No project carries two or more risk signals, no task is overdue "
            "and no department is running hot for this period.",
            module="All modules", metric="Alerts: 0",
            route="analytics:dashboard"))

    order = {"critical": 0, "warning": 1, "monitor": 2, "healthy": 3}
    found.sort(key=lambda a: order[a["level"]])
    return found


def _alert(level, title, body, *, module, metric, route):
    """`route` is a URL *name*, resolved by `{% url %}` in the template rather
    than a string built here — a hardcoded path silently rots the day somebody
    remounts an app in config/urls.py, and a dead link on an alert card is
    worse than no link."""
    return {"level": level, "title": title, "body": body,
            "module": module, "metric": metric, "route": route}


# ---------------------------------------------------------------------------
# the daily brief
# ---------------------------------------------------------------------------

def management_brief(filters, *, include_finance, projects, clients,
                     hours, task_counts, finance):
    """Short factual lines, each one a count somebody can go and check.

    Deliberately not prose and deliberately not generated by `ai_engine` — this
    is the part of the page that has to be true even when the AI key is
    missing, rate-limited or the model is having an imaginative day, so it is
    arithmetic all the way down.
    """
    today = timezone.localdate()
    items = []

    due_today = selectors.open_tasks(filters).filter(due_date=today).count()
    if due_today:
        items.append(f"{due_today} task{'' if due_today == 1 else 's'} "
                     f"{'is' if due_today == 1 else 'are'} due today.")
    if task_counts["overdue"]:
        n = task_counts["overdue"]
        items.append(f"{n} open task{'' if n == 1 else 's'} "
                     f"{'is' if n == 1 else 'are'} already overdue.")
    if task_counts["done"]:
        n = task_counts["done"]
        items.append(f"{n} task{'' if n == 1 else 's'} "
                     f"{'was' if n == 1 else 'were'} completed in this period.")

    at_risk = [row for row in projects if row["risk"]["level"] == "HIGH"]
    if at_risk:
        names = ", ".join(row["project"].name for row in at_risk[:3])
        items.append(f"{len(at_risk)} project"
                     f"{'' if len(at_risk) == 1 else 's'} "
                     f"{'is' if len(at_risk) == 1 else 'are'} carrying multiple "
                     f"risk signals: {names}.")

    pending = hours.get("pending_count", 0)
    if pending:
        items.append(f"{pending} work-log entr{'y' if pending == 1 else 'ies'} "
                     f"{'is' if pending == 1 else 'are'} waiting on an approver.")
    if hours["total"]:
        billable_pct = services._percent(hours["billable"], hours["total"])
        items.append(f"{hours['total']:,} hours logged, "
                     f"{billable_pct:.0f}% of them billable.")

    idle = [row for row in clients if row["active_projects"] == 0]
    if idle:
        items.append(f"{len(idle)} client{'' if len(idle) == 1 else 's'} "
                     f"{'has' if len(idle) == 1 else 'have'} no live project "
                     f"running.")

    if include_finance and finance["revenue"]:
        items.append(f"{finance['revenue']:,.0f} received across "
                     f"{finance['payment_count']} payment"
                     f"{'' if finance['payment_count'] == 1 else 's'}; "
                     f"{finance['outstanding']:,.0f} still outstanding.")

    if not items:
        items.append("No activity recorded in this period.")
    return items


# ---------------------------------------------------------------------------
# department scorecards
# ---------------------------------------------------------------------------

def department_scorecards(departments):
    """`services.department_metrics` rows plus the score and band the cards
    render. Scored on the same utilisation curve the composite uses, so a
    department reading "monitor" here is one the headline score is already
    docking points for."""
    cards = []
    for row in departments:
        distance = abs(row["utilization_percent"] - UTILISATION_IDEAL)
        score = max(0.0, 100.0 * (1 - distance / UTILISATION_TOLERANCE))
        cards.append({**row,
                      "score": int(round(score)),
                      "band": band_for(score)})
    cards.sort(key=lambda c: c["score"], reverse=True)
    return cards
