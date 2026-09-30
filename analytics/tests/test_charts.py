"""Chart payloads must be JSON-serialisable, labelled, and readable without
colour."""
import json
from datetime import date
from decimal import Decimal

from django.test import RequestFactory, TestCase

from analytics import charts, selectors, services

from .factories import Scenario


def _every_chart():
    """Every chart the analytics dashboard builds, with finance included.

    Used by the sweeps below so a chart added later is covered automatically
    rather than only the ones somebody remembered to name.
    """
    Scenario()
    filters = selectors.Filters.from_request(RequestFactory().get("/analytics/"))
    return charts.build_all(
        hours=selectors.hours_breakdown(filters),
        task_counts=selectors.task_status_counts(filters),
        hour_series=selectors.daily_hours_series(filters),
        revenue_series=selectors.monthly_revenue_series(filters),
        growth_series=selectors.monthly_growth_series(filters),
        projects=services.project_metrics(filters),
        employees=services.employee_metrics(filters),
        departments=services.department_metrics(filters),
    )


class SerialisationTests(TestCase):
    """Decimal is not JSON-serialisable. A chart that renders fine in a unit
    test and 500s in the template is the failure mode this guards."""

    def test_decimals_become_floats(self):
        series = [(date(2026, 1, 1), Decimal("1000.50"), 2)]
        payload = charts.revenue_trend(series)
        json.dumps(payload)  # raises TypeError if a Decimal survived
        self.assertEqual(payload["data"]["datasets"][0]["data"], [1000.5])

    def test_none_becomes_zero(self):
        self.assertEqual(charts._f(None), 0.0)

    def test_every_chart_survives_json_dumps(self):
        payload = _every_chart()
        # Nine since departments split into hours and tasks — the pair that
        # used to be one dual-axis chart.
        self.assertEqual(len(json.loads(json.dumps(payload))), 9)


class AccessibilityTests(TestCase):
    def test_line_series_are_distinguished_by_dash_not_only_colour(self):
        """Colour-blind readers get the same information."""
        payload = charts.hours_trend([(date(2026, 1, 1), Decimal("4"), Decimal("2"))])
        datasets = payload["data"]["datasets"]
        self.assertEqual(datasets[0]["borderDash"], [])
        self.assertNotEqual(datasets[1]["borderDash"], [])

    def test_every_dataset_is_labelled(self):
        payload = charts.hours_trend([(date(2026, 1, 1), Decimal("4"), Decimal("2"))])
        for dataset in payload["data"]["datasets"]:
            self.assertTrue(dataset["label"])

    def test_hover_target_is_larger_than_the_visible_point(self):
        """pointRadius 0 keeps a 90-day line clean, but the hit area has to stay
        big enough to actually hit."""
        payload = charts.hours_trend([(date(2026, 1, 1), Decimal("4"), Decimal("2"))])
        dataset = payload["data"]["datasets"][0]
        self.assertEqual(dataset["pointRadius"], 0)
        self.assertGreaterEqual(dataset["pointHitRadius"], 10)

    def test_entrance_animates_but_a_filter_change_stays_quick(self):
        """Two different jobs, two different durations.

        The entrance is allowed to be seen — it shows marks growing from the
        baseline, which is what makes the chart read as data arriving. A
        transition after a filter change is not: the reader just used a
        control and is waiting on the answer, so it is deliberately faster
        than the old single 300 ms setting rather than slower.
        """
        payload = charts.task_status(
            {"todo": 1, "in_progress": 0, "submitted": 0, "done": 2, "overdue": 0})
        entrance = payload["options"]["animation"]["duration"]
        self.assertGreaterEqual(entrance, 150)
        self.assertLessEqual(entrance, 700)
        transition = (payload["options"]["transitions"]["active"]
                      ["animation"]["duration"])
        self.assertLessEqual(transition, 250)


class ChartShapeTests(TestCase):
    def test_task_status_labels_match_the_board_columns(self):
        payload = charts.task_status(
            {"todo": 3, "in_progress": 2, "submitted": 1, "done": 5, "overdue": 4})
        self.assertEqual(payload["data"]["labels"],
                         ["To do", "In progress", "In review", "Done", "Overdue"])
        self.assertEqual(payload["data"]["datasets"][0]["data"],
                         [3.0, 2.0, 1.0, 5.0, 4.0])

    def test_billable_split_is_a_two_slice_doughnut(self):
        payload = charts.billable_split(
            {"billable": Decimal("12"), "non_billable": Decimal("6")})
        self.assertEqual(payload["type"], "doughnut")
        self.assertEqual(payload["data"]["datasets"][0]["data"], [12.0, 6.0])

    def test_project_health_is_horizontal_and_capped_at_100(self):
        rows = [{"project": type("P", (), {"name": "A"})(), "health": 40}]
        payload = charts.project_health(rows)
        self.assertEqual(payload["options"]["indexAxis"], "y")
        self.assertEqual(payload["options"]["scales"]["x"]["max"], 100)

    def test_project_health_limit_truncates(self):
        rows = [{"project": type("P", (), {"name": f"P{i}"})(), "health": i}
                for i in range(30)]
        payload = charts.project_health(rows, limit=5)
        self.assertEqual(len(payload["data"]["labels"]), 5)

    def test_health_colour_bands(self):
        self.assertEqual(charts._health_color(90), charts.OK)
        self.assertEqual(charts._health_color(60), charts.WARN)
        self.assertEqual(charts._health_color(20), charts.DANGER)

    def test_overwork_is_coloured_as_a_warning_not_a_success(self):
        """130% utilisation is a problem, so it must not be green."""
        self.assertEqual(charts._utilization_color(130), charts.DANGER)
        self.assertEqual(charts._utilization_color(85), charts.OK)

    def test_no_chart_carries_a_second_y_axis(self):
        """The dual-axis charts are gone, and must not come back.

        Two y-scales let whoever picks the scales decide which series looks
        like it is winning — the reader cannot tell a real crossover from a
        chosen one. Departments became two charts; growth became one indexed
        axis. This walks every chart the dashboard builds rather than naming
        the two that used to offend, so a new one cannot reintroduce it.
        """
        payload = _every_chart()
        for name, config in payload.items():
            scales = config.get("options", {}).get("scales", {})
            self.assertNotIn("y1", scales, f"{name} has a second y-axis")
            for dataset in config["data"]["datasets"]:
                self.assertNotIn("yAxisID", dataset,
                                 f"{name} pins a dataset to its own axis")

    def test_departments_are_two_charts_one_measure_each(self):
        rows = [{"department": type("D", (), {"name": "Delivery", "pk": 1})(),
                 "hours": Decimal("400"), "tasks_completed": 12}]
        hours = charts.department_hours(rows)
        tasks = charts.department_tasks(rows)
        self.assertEqual(len(hours["data"]["datasets"]), 1)
        self.assertEqual(len(tasks["data"]["datasets"]), 1)
        self.assertEqual(hours["data"]["datasets"][0]["data"], [400.0])
        self.assertEqual(tasks["data"]["datasets"][0]["data"], [12.0])

    def test_monthly_growth_indexes_every_series_to_100_at_its_start(self):
        """Indexing is what makes one axis honest for three different units."""
        payload = charts.monthly_growth([
            (date(2026, 1, 1), Decimal("1000"), Decimal("20"), 4),
            (date(2026, 2, 1), Decimal("1500"), Decimal("30"), 2),
        ])
        for dataset in payload["data"]["datasets"]:
            self.assertEqual(dataset["data"][0], 100.0)
        by_label = {d["label"]: d["data"] for d in payload["data"]["datasets"]}
        self.assertEqual(by_label["Revenue"][1], 150.0)   # 1500 / 1000
        self.assertEqual(by_label["Hours"][1], 150.0)     # 30 / 20
        self.assertEqual(by_label["Tasks done"][1], 50.0)  # 2 / 4

    def test_an_all_zero_series_is_dropped_rather_than_divided_by_zero(self):
        payload = charts.monthly_growth([
            (date(2026, 1, 1), Decimal("0"), Decimal("20"), 0),
            (date(2026, 2, 1), Decimal("0"), Decimal("30"), 0),
        ])
        labels = {d["label"] for d in payload["data"]["datasets"]}
        self.assertEqual(labels, {"Hours"})

    def test_clicking_a_project_bar_opens_that_project(self):
        rows = [{"project": type("P", (), {
                    "name": "A", "get_absolute_url": lambda self: "/projects/7/"})(),
                 "health": 40}]
        payload = charts.project_health(rows)
        self.assertEqual(payload["options"]["_drill"], ["/projects/7/"])

    def test_a_row_with_no_page_of_its_own_is_left_inert(self):
        rows = [{"project": type("P", (), {"name": "A"})(), "health": 40}]
        payload = charts.project_health(rows)
        self.assertEqual(payload["options"]["_drill"], [None])

    def test_currency_charts_are_flagged_for_the_client_formatter(self):
        payload = charts.revenue_trend([(date(2026, 1, 1), Decimal("5"), 1)])
        self.assertTrue(payload["options"]["_currency"])

    def test_finance_charts_omitted_when_finance_is_hidden(self):
        payload = charts.build_all(
            hours={"billable": Decimal("1"), "non_billable": Decimal("1")},
            task_counts={"todo": 0, "in_progress": 0, "submitted": 0,
                         "done": 0, "overdue": 0},
            hour_series=[], revenue_series=[], growth_series=[],
            projects=[], employees=[], departments=[],
            include_finance=False)
        self.assertNotIn("revenue-trend", payload)
        self.assertNotIn("monthly-growth", payload)
        self.assertIn("hours-trend", payload)

    def test_empty_data_still_produces_a_valid_chart(self):
        payload = charts.hours_trend([])
        self.assertEqual(payload["data"]["labels"], [])
        json.dumps(payload)
