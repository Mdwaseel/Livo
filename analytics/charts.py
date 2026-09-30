"""Chart.js payload builders.

Each function returns a plain dict that `json_script` drops into the template
and one shared initialiser feeds to `new Chart(...)`. Building the config in
Python rather than assembling it in JavaScript means the numbers are formatted
once, on the server, by the same code that computed them.

Rules that run through all of these:

* **Never encode meaning in colour alone.** Line series get distinct dash
  patterns, and every chart is paired with a visible figure or table in the
  template — a colour-blind reader and a screen-reader user both have a path to
  the value that doesn't go through the canvas.
* **One axis, always.** No chart here carries two y-scales. Two measures of
  different magnitude become two charts, or are indexed to a common base — a
  second axis lets the author decide which line "wins" by choosing the scales,
  which is the most common way a dashboard misleads.
* **The categorical palette is validated, not chosen by eye.** `SERIES` below
  passes the six colour checks against the white card surface: lightness band,
  chroma floor, colour-vision separation, normal-vision separation, and 3:1
  contrast. The previous palette did not — teal↔slate↔info all read as grey,
  and green↔red sat at ΔE 3.8 under deuteranopia, which is to say identical for
  a red-green colour-blind reader. The order is part of the result: adjacent
  slots are what the checks measure, so the sequence is not cosmetic and should
  not be re-ordered without re-running the validator.
* **Status colours are reserved.** `OK`/`WARN`/`DANGER` mean state — healthy,
  at risk, overdue — and are never reused as "series 4". They always ship with
  a label on the category axis, never as colour alone.
"""
from __future__ import annotations

from decimal import Decimal

# The validated categorical palette. Opens on the console's own brand pair
# (teal → amber) and then steps away in hue. Verified against the white card
# surface: worst adjacent separation ΔE 11.3 under protanopia (target ≥ 8) and
# ΔE 21.6 under normal vision (floor 15), every slot above the chroma floor,
# every slot ≥ 3:1 on white.
TEAL = "#00795b"
AMBER = "#c9820a"
VIOLET = "#5b4bc4"
MAGENTA = "#d6478f"
BLUE = "#2a78d6"
RUST = "#d2542a"
SERIES_COLORS = [TEAL, AMBER, VIOLET, MAGENTA, BLUE, RUST]

# Area fills for the first two slots. Kept faint so an overlapping line stays
# readable through them.
TEAL_SOFT = "rgba(0,121,91,.16)"
AMBER_SOFT = "rgba(201,130,10,.18)"

# Reserved for state, never for series identity. These are the app's own status
# tokens from app.css, so a chart agrees with the tags on the board it describes.
OK = "#128a48"
WARN = "#a36908"
DANGER = "#c22f2f"
INFO = "#20657f"
SLATE = "#5b6560"

# How many rows the dashboard's two ranked charts plot.
#
# These are shared with analytics/views.py, which slices the tables beneath
# those charts to the same length, and that is the whole reason they are
# constants. The charts drilled through on click while the tables listed eight
# rows, so bars nine through fifteen navigated somewhere no link pointed —
# clickable, and unreachable without a mouse. A chart and its paired table have
# to cover the same rows.
DASHBOARD_HEALTH_ROWS = 12
DASHBOARD_UTILIZATION_ROWS = 15

# Solid / dashed / dotted — the redundant encoding that makes the lines
# readable without colour.
SERIES_DASHES = [[], [6, 4], [2, 3], [10, 4], [4, 2, 2, 2], [1, 3]]


def _f(value):
    """Decimals are not JSON-serialisable and float() on None is a TypeError."""
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _line_dataset(index, label, data, *, fill=False):
    return {
        "label": label,
        "data": [_f(v) for v in data],
        "borderColor": SERIES_COLORS[index % len(SERIES_COLORS)],
        "backgroundColor": (TEAL_SOFT if index == 0 else AMBER_SOFT) if fill
                           else SERIES_COLORS[index % len(SERIES_COLORS)],
        "borderDash": SERIES_DASHES[index % len(SERIES_DASHES)],
        "borderWidth": 2,
        "fill": fill,
        "tension": 0.3,
        # Monotone, not the default cubic. With plain cubic smoothing a single
        # non-zero month among zeros is drawn as a smooth bell that rises above
        # the only real value and sags below zero on the way out -- the curve
        # asserts amounts that were never received. Monotone interpolation is
        # constrained to stay within the actual data points, so the line can
        # still be a curve without being a claim.
        "cubicInterpolationMode": "monotone",
        "pointRadius": 0,
        # Points appear on hover only — at 90 days a dot per day is noise, but
        # the hover target still has to be big enough to hit.
        "pointHoverRadius": 4,
        "pointHitRadius": 12,
    }


def _bar_dataset(index, label, data, *, colors=None):
    return {
        "label": label,
        "data": [_f(v) for v in data],
        "backgroundColor": colors or SERIES_COLORS[index % len(SERIES_COLORS)],
        "borderRadius": 4,
        "borderWidth": 0,
        "maxBarThickness": 46,
    }


def _short_date(value):
    return value.strftime("%d %b")


def _short_month(value):
    return value.strftime("%b %y")


# ---------------------------------------------------------------------------
# the eight required charts
# ---------------------------------------------------------------------------

def revenue_trend(series):
    """Monthly revenue. Line, because the question is "which way is it going"."""
    return {
        "type": "line",
        "data": {
            "labels": [_short_month(month) for month, _, _ in series],
            "datasets": [_line_dataset(0, "Revenue (₹)",
                                       [amount for _, amount, _ in series],
                                       fill=True)],
        },
        "options": _options(y_label="₹", currency=True),
    }


def hours_trend(series):
    """Daily hours, billable and non-billable as separate lines rather than a
    stack: the reader's question is "how much billable work happened", and a
    stacked total makes that the hard number to read."""
    return {
        "type": "line",
        "data": {
            "labels": [_short_date(day) for day, _, _ in series],
            "datasets": [
                _line_dataset(0, "Billable", [b for _, b, _ in series], fill=True),
                _line_dataset(1, "Non-billable", [n for _, _, n in series]),
            ],
        },
        "options": _options(y_label="hours"),
    }


def billable_split(hours):
    """Two-slice doughnut. Part-to-whole with two parts is the one case a
    doughnut genuinely beats a bar."""
    return {
        "type": "doughnut",
        "data": {
            "labels": ["Billable", "Non-billable"],
            "datasets": [{
                "data": [_f(hours["billable"]), _f(hours["non_billable"])],
                "backgroundColor": [TEAL, AMBER],
                "borderWidth": 2,
                "borderColor": "#ffffff",
            }],
        },
        "options": _doughnut_options(),
    }


def task_status(counts):
    """Board columns as a bar. Status colours match the board's own tags, so a
    reader who knows the kanban already knows this chart."""
    return {
        "type": "bar",
        "data": {
            "labels": ["To do", "In progress", "In review", "Done", "Overdue"],
            "datasets": [_bar_dataset(0, "Tasks", [
                counts["todo"], counts["in_progress"],
                counts["submitted"], counts["done"], counts["overdue"],
            ], colors=[SLATE, INFO, WARN, OK, DANGER])],
        },
        "options": _options(y_label="tasks", legend=False),
    }


def project_health(rows, *, limit=DASHBOARD_HEALTH_ROWS):
    """Horizontal bars, worst first — a health chart is read to find the
    problems, so the problems go at the top where the eye lands.

    Clicking a bar opens that project. A health score is a prompt to go and
    look at something, so the chart is the shortest path to the thing itself
    rather than a picture you then navigate away from by hand.
    """
    rows = rows[:limit]
    return {
        "type": "bar",
        "data": {
            "labels": [r["project"].name for r in rows],
            "datasets": [_bar_dataset(0, "Health score",
                                      [r["health"] for r in rows],
                                      colors=[_health_color(r["health"])
                                              for r in rows])],
        },
        "options": _options(y_label="score", legend=False, horizontal=True,
                            y_max=100,
                            drill=[_object_url(r["project"]) for r in rows]),
    }


def employee_utilization(rows, *, limit=DASHBOARD_UTILIZATION_ROWS,
                         querystring=""):
    """Utilisation against the 100% full-time line. Bars are coloured by band
    and every bar is labelled with its number in the table beside it, so the
    banding is reinforcement rather than the only signal."""
    rows = rows[:limit]
    return {
        "type": "bar",
        "data": {
            "labels": [_person_label(r["user"]) for r in rows],
            "datasets": [_bar_dataset(0, "Utilisation %",
                                      [r["utilization_percent"] for r in rows],
                                      colors=[_utilization_color(
                                          r["utilization_percent"]) for r in rows])],
        },
        "options": _options(y_label="%", legend=False, horizontal=True,
                            drill=[employee_url(r["user"], querystring)
                                   for r in rows]),
    }


def department_hours(rows, *, drill=None):
    """Hours logged per department.

    Split from the task count rather than sharing a chart with it. The two
    measures differ by an order of magnitude — 400 hours against 12 tasks — and
    the old second y-axis made their relative heights an artefact of the scales
    somebody picked. Two charts, one axis each, is the honest form: the reader
    compares departments within a measure, which is the actual question.
    """
    return {
        "type": "bar",
        "data": {
            "labels": [r["department"].name for r in rows],
            "datasets": [_bar_dataset(0, "Hours logged",
                                      [r["hours"] for r in rows])],
        },
        "options": _options(y_label="hours", legend=False, drill=drill),
    }


def department_tasks(rows, *, drill=None):
    """Tasks completed per department — the other half of the pair above."""
    return {
        "type": "bar",
        "data": {
            "labels": [r["department"].name for r in rows],
            "datasets": [_bar_dataset(1, "Tasks completed",
                                      [r["tasks_completed"] for r in rows])],
        },
        "options": _options(y_label="tasks", legend=False, drill=drill),
    }


# ---------------------------------------------------------------------------
# the home dashboard
#
# These three carry no money. That is the entire reason the dashboard has
# charts again: the figures that could not be shown to a room moved to the
# Business overview, and what is left — where work sits, what is on the board,
# what is at risk — is exactly what the dashboard should have been showing all
# along.
# ---------------------------------------------------------------------------

def project_pipeline(rows, *, drill=None):
    """How many projects sit at each stage, in the order work moves through.

    Ordered by the pipeline itself rather than by count, because the shape is
    the message: a stack at Proposal Sent and nothing Active is a different
    problem from the reverse, and sorting by size would hide both.
    """
    return {
        "type": "bar",
        "data": {
            "labels": [label for label, _ in rows],
            "datasets": [_bar_dataset(0, "Projects",
                                      [count for _, count in rows])],
        },
        "options": _options(y_label="projects", legend=False, drill=drill),
    }


def task_board(rows, *, drill=None):
    """The board as a bar chart: to do, in progress, done."""
    return {
        "type": "bar",
        "data": {
            "labels": [label for label, _ in rows],
            "datasets": [_bar_dataset(2, "Tasks",
                                      [count for _, count in rows])],
        },
        "options": _options(y_label="tasks", legend=False, drill=drill),
    }


# Draft → In Review → Approved → Sent, in the order documents/models.py
# declares them. These are STATES, so they wear the status tokens rather than
# the categorical palette: "in review" carries the same amber as every other
# thing waiting on somebody elsewhere in the console.
DOCUMENT_STATE_COLORS = [SLATE, WARN, OK, INFO]


def document_pipeline(rows, *, drill=None):
    """Documents by state — drafts, in review, and what is signed off.

    Status colours rather than the categorical palette: these are states, not
    identities, and "in review" carrying the same amber as every other warning
    in the console is the point.
    """
    return {
        "type": "bar",
        "data": {
            "labels": [label for label, _, _ in rows],
            "datasets": [{
                **_bar_dataset(0, "Documents", [count for _, count, _ in rows]),
                "backgroundColor": [color for _, _, color in rows],
            }],
        },
        "options": _options(y_label="documents", legend=False,
                            horizontal=True, drill=drill),
    }


def _indexed(values):
    """Rebase a series to 100 at its first non-zero point.

    Returns None when there is nothing to rebase from — an all-zero series has
    no growth to show, and dividing by its base would be a division by zero
    dressed up as a flat line at 100.
    """
    base = next((v for v in values if v), None)
    if not base:
        return None
    return [round(_f(v) / _f(base) * 100, 1) for v in values]


def monthly_growth(series):
    """Revenue, hours and completed tasks on ONE axis, each indexed to 100 at
    its own starting month.

    The question this chart is asked is "which is growing faster" — and that is
    a question about rates, not absolute values. Indexing answers it directly
    and, unlike the two-axis version this replaces, cannot be made to say
    something different by rescaling an axis. The cost is that the axis reads
    "relative to the start" rather than in rupees or hours; the revenue chart
    beside it carries the absolute figures.
    """
    months = [_short_month(month) for month, _, _, _ in series]
    tracks = [
        ("Revenue", [revenue for _, revenue, _, _ in series]),
        ("Hours", [hours for _, _, hours, _ in series]),
        ("Tasks done", [tasks for _, _, _, tasks in series]),
    ]
    datasets = []
    for label, values in tracks:
        indexed = _indexed(values)
        if indexed is None:
            continue
        datasets.append(_line_dataset(len(datasets), label, indexed))
    return {
        "type": "line",
        "data": {"labels": months, "datasets": datasets},
        "options": _options(y_label="indexed to 100 at start"),
    }


# ---------------------------------------------------------------------------
# business overview
# ---------------------------------------------------------------------------

def client_revenue(rows, *, limit=10):
    """Billed vs collected per client, as paired horizontal bars.

    Grouped rather than stacked: the reader's question is "how much of what we
    agreed is actually in", which is a comparison of two lengths from a shared
    baseline. Stacking them would make collected a segment of billed, and the
    gap — the part that matters — would have to be measured by eye.

    Clicking a bar opens that client.
    """
    rows = rows[:limit]
    return {
        "type": "bar",
        "data": {
            "labels": [r["client"].name for r in rows],
            "datasets": [
                _bar_dataset(0, "Collected", [r.get("revenue") for r in rows]),
                _bar_dataset(1, "Outstanding",
                             [r.get("outstanding") for r in rows]),
            ],
        },
        "options": _options(y_label="₹", horizontal=True, currency=True,
                            drill=[_object_url(r["client"]) for r in rows]),
    }


def collection_gauge(finance):
    """Collected against still-owed, as a two-slice doughnut.

    A part-to-whole with two parts, which is the one case a doughnut genuinely
    beats a bar. The centre figure is rendered in the template rather than on
    the canvas — text baked into a chart image is invisible to a screen reader
    and does not reflow.
    """
    collected = _f(finance.get("collected_all_time"))
    outstanding = max(_f(finance.get("outstanding")), 0.0)
    return {
        "type": "doughnut",
        "data": {
            "labels": ["Collected", "Outstanding"],
            "datasets": [{
                "data": [collected, outstanding],
                # Collected is the brand teal; outstanding takes the status
                # warning colour, because money not yet in is a state rather
                # than a second category of thing.
                "backgroundColor": [TEAL, WARN],
                "borderWidth": 2,
                "borderColor": "#ffffff",
            }],
        },
        "options": _doughnut_options(currency=True),
    }


# ---------------------------------------------------------------------------
# shared option blocks
# ---------------------------------------------------------------------------

def _person_label(user):
    return user.get_full_name() or user.get_username()


def _object_url(obj):
    """`get_absolute_url()` if the object has one, else None.

    Returning None rather than raising keeps a chart renderable for anything
    that is shaped like a row but has no page of its own to open — the
    initialiser leaves those marks inert.
    """
    getter = getattr(obj, "get_absolute_url", None)
    return getter() if callable(getter) else None


def _with_param(querystring, name, value):
    """Add or replace one filter in an existing querystring.

    Drill-through keeps the filters already on screen — clicking a bar should
    narrow what you are looking at, not silently reset the date range you
    chose.
    """
    from urllib.parse import parse_qs, urlencode

    params = parse_qs(querystring or "", keep_blank_values=False)
    params[name] = [str(value)]
    return urlencode(params, doseq=True)


def employee_url(user, querystring=""):
    """The employees page, narrowed to one person, keeping the current range.

    Public because the drill-through target and the table link under the chart
    have to be the same URL — see analytics/templatetags/analytics_tags.py.
    """
    from django.urls import reverse

    return (f"{reverse('analytics:employees')}"
            f"?{_with_param(querystring, 'employee', user.pk)}")


def department_url(department, querystring=""):
    from django.urls import reverse

    return (f"{reverse('analytics:dashboard')}"
            f"?{_with_param(querystring, 'department', department.pk)}")


def _health_color(score):
    return OK if score >= 75 else WARN if score >= 50 else DANGER


def _utilization_color(percent):
    # Over 100% is overwork, not excellence — it gets the warning colour, not
    # the good one.
    if percent > 110:
        return DANGER
    if percent >= 70:
        return OK
    return WARN if percent >= 40 else SLATE


def _base_options():
    return {
        "responsive": True,
        "maintainAspectRatio": False,
        # Marks grow from the baseline and the series stagger in, so a chart
        # reads as "these values arriving" rather than a flash of finished
        # picture. 600 ms with a per-mark delay stays inside the band where
        # motion conveys structure without being something you wait through;
        # the template zeroes the whole block under prefers-reduced-motion.
        "animation": {"duration": 600, "easing": "easeOutQuart"},
        # No `animations` override here, and that is load-bearing rather than an
        # omission. It used to carry `{"y": {"from": None}}`, meaning "grow from
        # the baseline" — but None serialises to JSON null, and Chart.js picks
        # its interpolator with `interpolators[cfg.type || typeof from]`.
        # `typeof null` is "object", there is no object interpolator, so `_fn`
        # came out undefined and every chart threw "this._fn is not a function"
        # part-way through its first draw. The canvas was left blank with the
        # page otherwise fine, which is why this survived so long: the server
        # rendered a perfect payload and the browser silently dropped it.
        # Chart.js already grows bars from the baseline on its own, so the
        # intent survives without the override. If this block ever comes back,
        # `from` must be a NUMBER (or a function returning one), never null.
        "transitions": {
            # Filter changes are a *change*, not an entrance — they animate
            # faster so the page feels responsive to the control just used.
            "active": {"animation": {"duration": 180}},
        },
        "interaction": {"mode": "index", "intersect": False},
        "plugins": {
            "legend": {
                "display": True,
                "position": "bottom",
                "labels": {"usePointStyle": True, "boxWidth": 8,
                           "padding": 14, "font": {"size": 11}},
            },
            "tooltip": {
                "backgroundColor": "#0b0f0d",
                "padding": 10,
                "cornerRadius": 8,
                "titleFont": {"size": 12},
                "bodyFont": {"size": 12},
                "displayColors": True,
            },
        },
    }


def has_data(payload):
    """Is there anything in this chart worth drawing?

    Chart.js will happily render a config whose every value is zero, and what
    it draws is an empty grid — or, for a doughnut, nothing whatsoever. On a
    fresh install, or an agency where nobody has logged time yet, that is most
    of the page: blank rectangles with titles over them, which read as broken
    software rather than as an absence of data.

    So the card asks this first and shows a sentence instead. "No hours logged
    in this range" is a fact; a blank canvas is a bug report waiting to happen.
    """
    if not payload:
        return False
    for dataset in (payload.get("data") or {}).get("datasets") or []:
        for value in dataset.get("data") or []:
            if isinstance(value, (int, float)) and value:
                return True
    return False


def _options(*, y_label="", legend=True, horizontal=False, currency=False,
             y_max=None, drill=None):
    options = _base_options()
    options["plugins"]["legend"]["display"] = legend
    if horizontal:
        options["indexAxis"] = "y"
        options["interaction"] = {"mode": "nearest", "intersect": True}
    value_axis = "x" if horizontal else "y"
    category_axis = "y" if horizontal else "x"
    options["scales"] = {
        value_axis: {
            "beginAtZero": True,
            "title": {"display": bool(y_label), "text": y_label,
                      "font": {"size": 11}},
            "grid": {"color": "rgba(11,15,13,.06)"},
            "ticks": {"font": {"size": 11}, "precision": 0},
        },
        category_axis: {
            "grid": {"display": False},
            "ticks": {"font": {"size": 11}, "autoSkip": True,
                      "maxRotation": 0},
        },
    }
    if y_max is not None:
        options["scales"][value_axis]["max"] = y_max
    options["_currency"] = currency  # read by the template initialiser
    # One URL per category, parallel to `labels`. The initialiser turns these
    # into a click handler and a pointer cursor; `None` entries stay inert, so a
    # chart is only clickable where there is genuinely something to open.
    if drill:
        options["_drill"] = list(drill)
    return options


def _doughnut_options(*, currency=False):
    options = _base_options()
    options["cutout"] = "62%"
    options["interaction"] = {"mode": "nearest", "intersect": True}
    options["_currency"] = currency
    return options


# ---------------------------------------------------------------------------
# executive dashboard
#
# Four charts that only earn their place next to each other: the funnel, the
# customer mix, whether the board is filling faster than it empties, and how
# the departments compare. Each is built from a `selectors`/`services` rollup
# and adds no arithmetic of its own.
# ---------------------------------------------------------------------------

# Lead -> Proposal -> Active -> On hold -> Completed, cool to warm, with the
# stalled stage in amber and the won stage in green. Positional, not
# categorical: the reader is meant to see progression along the row.
PIPELINE_STAGE_COLORS = [SLATE, INFO, TEAL, WARN, OK]


# Short forms for the category axis. The full choice labels ("Proposal Sent")
# are wider than Chart.js reserves for tick text on a one-third-width card, and
# the overflow is CLIPPED rather than wrapped -- the axis read "roposal Sent"
# in production. Shortening here keeps the fix in the chart that has the
# constraint, rather than renaming the status everywhere in the app.
PIPELINE_SHORT_LABELS = {
    "LEAD": "Lead",
    "PROPOSAL": "Proposal",
    "ACTIVE": "Active",
    "ON_HOLD": "On hold",
    "COMPLETED": "Won",
}


def pipeline_stages(rows, *, currency=True):
    """Value by stage, horizontal so the stage names read as a funnel.

    Bars are drawn from `value`, not `count`, because the executive question is
    how much money is sitting at each step. The count travels in the label so a
    stage holding one large project cannot be misread as holding several.
    """
    labels = [f"{PIPELINE_SHORT_LABELS.get(status, label)} ({count})"
              for status, label, count, _ in rows]
    values = [_f(value) for _, _, _, value in rows]
    return {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [_bar_dataset(0, "Value (₹)", values,
                                      colors=PIPELINE_STAGE_COLORS[:len(rows)])],
        },
        "options": _options(y_label="₹", legend=False, horizontal=True,
                            currency=currency),
    }


def client_health_bands(bands):
    """Healthy / monitor / at-risk as a doughnut — part-to-whole over three
    parts that sum to the client count, which is the one shape a doughnut is
    actually the right chart for."""
    return {
        "type": "doughnut",
        "data": {
            "labels": ["Healthy", "Monitor", "At risk"],
            "datasets": [{
                "data": [bands["healthy"], bands["monitor"], bands["risk"]],
                "backgroundColor": [OK, WARN, DANGER],
                "borderWidth": 2,
                "borderColor": "#ffffff",
            }],
        },
        "options": _doughnut_options(),
    }


def task_flow(series):
    """Created against completed, with the net movement behind them.

    Created and completed are bars because each week is a discrete count; the
    net is a line because it is a running position rather than a weekly event.
    Drawing all three as lines made the crossings look like the story, when the
    story is the gap between the two bars.
    """
    return {
        "type": "bar",
        "data": {
            "labels": [_short_date(week) for week, _, _, _ in series],
            "datasets": [
                _bar_dataset(0, "Created", [c for _, c, _, _ in series],
                             colors=[INFO] * len(series)),
                _bar_dataset(1, "Completed", [d for _, _, d, _ in series],
                             colors=[OK] * len(series)),
                {**_line_dataset(2, "Net movement",
                                 [b for _, _, _, b in series]),
                 "type": "line"},
            ],
        },
        "options": _options(y_label="tasks"),
    }


def utilisation_trend(series):
    """Utilisation and billable share by month, both as percentages.

    One axis because both are percentages of something, capped at 100 so the
    two lines are read against the same ceiling. Utilisation can genuinely
    exceed 100 — that is overwork, and `y_max` deliberately lets the line run
    past the top of the grid rather than rescaling the axis and hiding it.
    """
    return {
        "type": "line",
        "data": {
            "labels": [_short_month(month) for month, _, _ in series],
            "datasets": [
                _line_dataset(0, "Utilisation %", [u for _, u, _ in series],
                              fill=True),
                _line_dataset(1, "Billable share %", [b for _, _, b in series]),
            ],
        },
        "options": _options(y_label="%", y_max=100),
    }


def department_scorecard(rows):
    """One bar per department, coloured by band and capped at 100 so the bars
    are read against the same ceiling rather than against each other."""
    return {
        "type": "bar",
        "data": {
            "labels": [row["department"].name for row in rows],
            "datasets": [_bar_dataset(
                0, "Score",
                [row["score"] for row in rows],
                colors=[_health_color(row["score"]) for row in rows])],
        },
        "options": _options(y_label="score", legend=False, y_max=100),
    }


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

def build_all(*, hours, task_counts, hour_series, revenue_series, growth_series,
              projects, employees, departments, include_finance=True,
              querystring=""):
    """Every chart the dashboard needs, keyed by canvas id.

    Finance charts are omitted rather than zeroed for users without
    `finance.view` — an empty revenue chart still tells them revenue exists and
    is currently nothing, which is itself information they shouldn't have.

    `querystring` is the filter bar's current state, threaded through so a
    drill-through link narrows the view the reader already set up instead of
    resetting it.
    """
    department_drill = [department_url(r["department"], querystring)
                        for r in departments]
    charts = {
        "hours-trend": hours_trend(hour_series),
        "billable-split": billable_split(hours),
        "task-status": task_status(task_counts),
        "project-health": project_health(projects),
        "employee-utilization": employee_utilization(
            employees, querystring=querystring),
        "department-hours": department_hours(departments,
                                             drill=department_drill),
        "department-tasks": department_tasks(departments,
                                             drill=department_drill),
    }
    if include_finance:
        charts["revenue-trend"] = revenue_trend(revenue_series)
        charts["monthly-growth"] = monthly_growth(growth_series)
    return charts
