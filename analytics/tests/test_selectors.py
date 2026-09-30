"""Selector tests: filters bite, aggregates are right, nothing double-counts."""
from datetime import timedelta
from decimal import Decimal

from django.test import RequestFactory, TestCase
from django.utils import timezone

from analytics import selectors

from .factories import Scenario


class FiltersTests(TestCase):
    def setUp(self):
        self.rf = RequestFactory()

    def parse(self, query=""):
        return selectors.Filters.from_request(self.rf.get(f"/analytics/?{query}"))

    def test_defaults_to_a_90_day_window_ending_today(self):
        filters = self.parse()
        today = timezone.localdate()
        self.assertEqual(filters.end, today)
        self.assertEqual(filters.start, today - timedelta(days=90))

    def test_explicit_dates_are_honoured(self):
        filters = self.parse("start=2026-01-01&end=2026-01-31")
        self.assertEqual(filters.start.isoformat(), "2026-01-01")
        self.assertEqual(filters.days, 31)

    def test_backwards_range_is_swapped_not_left_empty(self):
        """Otherwise the page renders zeroes and reads as "no data" rather than
        "your dates are the wrong way round"."""
        filters = self.parse("start=2026-03-31&end=2026-03-01")
        self.assertEqual(filters.start.isoformat(), "2026-03-01")
        self.assertEqual(filters.end.isoformat(), "2026-03-31")

    def test_garbage_dates_fall_back_to_the_default_window(self):
        filters = self.parse("start=not-a-date&end=42")
        self.assertEqual(filters.days, 91)

    def test_garbage_ids_are_dropped_rather_than_crashing(self):
        filters = self.parse("employee=abc&project=-3&client=0")
        self.assertIsNone(filters.employee_id)
        self.assertIsNone(filters.project_id)
        self.assertIsNone(filters.client_id)
        self.assertFalse(filters.is_filtered)

    def test_querystring_round_trips_for_export_links(self):
        filters = self.parse("start=2026-01-01&end=2026-01-31&employee=4")
        query = filters.querystring()
        self.assertIn("start=2026-01-01", query)
        self.assertIn("employee=4", query)
        # Empty filters are omitted rather than sent as blanks.
        self.assertNotIn("project=", query)


class AggregateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def default_filters(self):
        return selectors.Filters.from_request(RequestFactory().get("/analytics/"))

    # --- hours ---

    def test_hours_breakdown_totals_match_the_fixture(self):
        hours = selectors.hours_breakdown(self.default_filters())
        self.assertEqual(hours["total"], Scenario.TEAM_TOTAL)
        self.assertEqual(hours["billable"], Scenario.TEAM_BILLABLE)
        self.assertEqual(hours["non_billable"], Decimal("6"))
        self.assertEqual(hours["entries"], 4)

    def test_parts_sum_to_the_total(self):
        """The whole reason the breakdown is one conditional-aggregate pass."""
        hours = selectors.hours_breakdown(self.default_filters())
        self.assertEqual(hours["billable"] + hours["non_billable"], hours["total"])

    def test_entries_outside_the_window_are_excluded(self):
        """The fixture logs 99 h 400 days ago; the default window is 90."""
        hours = selectors.hours_breakdown(self.default_filters())
        self.assertNotIn(Decimal("99"), hours.values())
        self.assertEqual(hours["total"], Decimal("24"))

    def test_employee_filter_narrows_hours(self):
        filters = self.default_filters()
        filters.employee_id = self.data.anna.pk
        self.assertEqual(
            selectors.hours_breakdown(filters)["total"], Scenario.ANNA_TOTAL)

    def test_department_filter_uses_the_logger_not_the_task(self):
        filters = self.default_filters()
        filters.department_id = self.data.design.pk
        self.assertEqual(
            selectors.hours_breakdown(filters)["total"], Scenario.BILAL_TOTAL)

    def test_client_filter_narrows_hours(self):
        filters = self.default_filters()
        filters.client_id = self.data.client_b.pk
        self.assertEqual(
            selectors.hours_breakdown(filters)["total"], Scenario.BILAL_TOTAL)

    def test_hours_on_a_single_day(self):
        filters = self.default_filters()
        yesterday = self.data.today - timedelta(days=1)
        # Anna 6 h non-billable + Bilal 6 h billable.
        self.assertEqual(selectors.hours_on(yesterday, base_filters=filters),
                         Decimal("12"))

    def test_pending_approvals_counts_only_submitted(self):
        self.assertEqual(
            selectors.pending_worklog_approvals(self.default_filters()), 1)

    # --- tasks ---

    def test_task_status_counts(self):
        counts = selectors.task_status_counts(self.default_filters())
        self.assertEqual(counts["done"], 3)
        self.assertEqual(counts["todo"], 1)
        self.assertEqual(counts["in_progress"], 1)
        self.assertEqual(counts["overdue"], 1)
        self.assertEqual(counts["total"], 5)

    def test_overdue_excludes_done_tasks(self):
        """A finished task with a past due date is finished, not overdue —
        matching Task.is_overdue."""
        counts = selectors.task_status_counts(self.default_filters())
        self.assertEqual(counts["overdue"], 1)

    def test_open_tasks_ignore_the_date_window(self):
        """An overdue task from last year is still overdue today."""
        filters = self.default_filters()
        filters.start = self.data.today
        filters.end = self.data.today
        self.assertEqual(selectors.open_tasks(filters).count(), 2)

    # --- money ---

    def test_revenue_excludes_archived_projects(self):
        """The fixture hides ₹777,777 on an archived project."""
        totals = selectors.revenue_totals(self.default_filters())
        self.assertEqual(totals["received"], Scenario.WEBSITE_RECEIVED)
        self.assertEqual(totals["payment_count"], 1)

    def test_portfolio_matches_budget_minus_received(self):
        portfolio = selectors.portfolio_totals(self.default_filters())
        self.assertEqual(portfolio["value"], Scenario.PORTFOLIO_VALUE)
        self.assertEqual(portfolio["received"], Scenario.WEBSITE_RECEIVED)
        self.assertEqual(portfolio["outstanding"], Scenario.PORTFOLIO_OUTSTANDING)

    def test_empty_aggregates_are_zero_not_none(self):
        """Coalesce in SQL, so nothing downstream has to guard against None."""
        filters = self.default_filters()
        filters.start = self.data.today + timedelta(days=10)
        filters.end = self.data.today + timedelta(days=20)
        self.assertEqual(selectors.revenue_totals(filters)["received"], Decimal("0"))
        self.assertEqual(selectors.hours_breakdown(filters)["total"], Decimal("0"))

    # --- per-entity rows ---

    def test_employee_rows_do_not_multiply_hours_by_task_count(self):
        """The join-fan-out trap: Anna has 3 logs and 4 tasks. A single query
        joining both would report 18 × 4 hours."""
        rows = {r["profile"].user_id: r
                for r in selectors.employee_rows(self.default_filters())}
        anna = rows[self.data.anna.pk]
        self.assertEqual(anna["logs"]["total_hours"], Scenario.ANNA_TOTAL)
        self.assertEqual(anna["completed"]["completed"], 2)

    def test_employee_rows_include_people_with_no_activity(self):
        """A zero row is a finding. Dropping it hides someone who logged
        nothing all month."""
        rows = selectors.employee_rows(self.default_filters())
        user_ids = {r["profile"].user_id for r in rows}
        self.assertIn(self.data.outsider.pk, user_ids)

    def test_project_rows_carry_hours_payments_tasks_and_milestones(self):
        rows = {r["project"].pk: r
                for r in selectors.project_rows(self.default_filters())}
        website = rows[self.data.website.pk]
        self.assertEqual(website["hours"]["actual"], Decimal("18"))
        self.assertEqual(website["payment"]["received"], Scenario.WEBSITE_RECEIVED)
        self.assertEqual(website["tasks"]["total"], 4)
        self.assertEqual(website["milestones"]["done"], 1)

    def test_project_rows_exclude_archived(self):
        names = {r["project"].name
                 for r in selectors.project_rows(self.default_filters())}
        self.assertNotIn("Old thing", names)

    def test_client_rows_aggregate_across_projects(self):
        rows = {r["client"].pk: r
                for r in selectors.client_rows(self.default_filters())}
        acme = rows[self.data.client_a.pk]
        # Acme has three projects: the one clients/signals.py auto-creates with
        # every client, "Website", and an archived one that must not count.
        self.assertEqual(acme["projects"]["projects"], 2)
        self.assertEqual(acme["payment"]["received"], Scenario.WEBSITE_RECEIVED)
        self.assertEqual(acme["documents"]["documents"], 1)

    def test_department_rows_split_by_logger(self):
        rows = {r["department"].name: r
                for r in selectors.department_rows(self.default_filters())}
        self.assertEqual(rows["Delivery"]["hours"]["total_hours"], Decimal("18"))
        self.assertEqual(rows["Design"]["hours"]["total_hours"], Decimal("6"))

    def test_rows_are_empty_lists_not_errors_when_nothing_matches(self):
        filters = self.default_filters()
        filters.client_id = 999999
        self.assertEqual(selectors.project_rows(filters), [])
        self.assertEqual(selectors.client_rows(filters), [])


class SeriesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def filters(self, days=30):
        today = timezone.localdate()
        start = today - timedelta(days=days)
        request = RequestFactory().get(f"/analytics/?start={start}&end={today}")
        return selectors.Filters.from_request(request)

    def test_daily_series_has_one_point_per_day_including_empty_ones(self):
        """A line chart that skips empty days draws a straight line across a
        fortnight of leave."""
        filters = self.filters(days=29)
        series = selectors.daily_hours_series(filters)
        self.assertEqual(len(series), 30)
        self.assertEqual([d for d, _, _ in series][-1], timezone.localdate())

    def test_daily_series_totals_match_the_breakdown(self):
        filters = self.filters(days=89)
        series = selectors.daily_hours_series(filters)
        self.assertEqual(sum(b for _, b, _ in series), Scenario.TEAM_BILLABLE)
        self.assertEqual(sum(n for _, _, n in series), Decimal("6"))

    def test_monthly_revenue_series_is_gap_filled(self):
        filters = self.filters(days=200)
        series = selectors.monthly_revenue_series(filters)
        self.assertGreaterEqual(len(series), 6)
        self.assertEqual(sum(amount for _, amount, _ in series),
                         Scenario.WEBSITE_RECEIVED)

    def test_growth_series_carries_all_three_signals(self):
        series = selectors.monthly_growth_series(self.filters(days=89))
        self.assertTrue(all(len(row) == 4 for row in series))
        self.assertEqual(sum(hours for _, _, hours, _ in series),
                         Scenario.TEAM_TOTAL)


class OptionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_filter_options_exclude_archived_records(self):
        options = selectors.filter_options()
        self.assertNotIn("Old thing",
                         [p.name for p in options["projects"]])
        self.assertEqual(options["departments"].count(), 2)


class FilterApplicationTests(TestCase):
    """Every filter, against every selector that claims to honour it.

    A filter that silently doesn't apply is invisible on screen — the page
    simply shows more than it should — so each dimension is asserted to
    actually narrow the result rather than merely not crash.
    """

    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def filters(self, **overrides):
        base = selectors.Filters.from_request(RequestFactory().get("/analytics/"))
        for key, value in overrides.items():
            setattr(base, key, value)
        return base

    # --- work_logs ---

    def test_work_logs_narrow_by_project(self):
        filters = self.filters(project_id=self.data.branding.pk)
        self.assertEqual(selectors.hours_breakdown(filters)["total"],
                         Scenario.BILAL_TOTAL)

    # --- tasks (creation window) ---

    def test_tasks_by_creation_window_excludes_older_rows(self):
        """The `by_completion=False` branch — "created this period", which is a
        different question from "completed this period"."""
        wide = self.filters()
        self.assertEqual(selectors.tasks(wide).count(), 5)
        future = self.filters(start=self.data.today + timedelta(days=1),
                              end=self.data.today + timedelta(days=5))
        self.assertEqual(selectors.tasks(future).count(), 0)

    def test_tasks_narrow_by_project_client_and_department(self):
        self.assertEqual(
            selectors.tasks(self.filters(project_id=self.data.branding.pk)).count(), 1)
        self.assertEqual(
            selectors.tasks(self.filters(client_id=self.data.client_b.pk)).count(), 1)
        self.assertEqual(
            selectors.tasks(self.filters(department_id=self.data.design.pk)).count(), 1)

    # --- open_tasks ---

    def test_open_tasks_narrow_by_project_and_client(self):
        self.assertEqual(
            selectors.open_tasks(self.filters(project_id=self.data.website.pk)).count(), 2)
        self.assertEqual(
            selectors.open_tasks(self.filters(client_id=self.data.client_b.pk)).count(), 0)

    # --- payments ---

    def test_payments_narrow_by_project_and_client(self):
        paid = self.filters(project_id=self.data.website.pk)
        self.assertEqual(selectors.revenue_totals(paid)["payment_count"], 1)
        unpaid = self.filters(project_id=self.data.branding.pk)
        self.assertEqual(selectors.revenue_totals(unpaid)["payment_count"], 0)
        by_client = self.filters(client_id=self.data.client_b.pk)
        self.assertEqual(selectors.revenue_totals(by_client)["payment_count"], 0)

    # --- projects ---

    def test_live_projects_narrow_to_a_single_project(self):
        filters = self.filters(project_id=self.data.website.pk)
        self.assertEqual(selectors.live_projects(filters).count(), 1)

    # --- task_status_counts ---

    def test_status_counts_narrow_on_every_dimension(self):
        for key, value, expected in (
            ("employee_id", self.data.bilal.pk, 1),
            ("project_id", self.data.branding.pk, 1),
            ("client_id", self.data.client_b.pk, 1),
            ("department_id", self.data.design.pk, 1),
        ):
            with self.subTest(filter=key):
                counts = selectors.task_status_counts(self.filters(**{key: value}))
                self.assertEqual(counts["total"], expected)

    # --- documents ---

    def test_documents_narrow_by_project_and_client(self):
        self.assertEqual(
            selectors.documents_generated(
                self.filters(project_id=self.data.website.pk)).count(), 1)
        self.assertEqual(
            selectors.documents_generated(
                self.filters(client_id=self.data.client_b.pk)).count(), 0)

    # --- department_rows ---

    def test_department_rows_narrow_to_one_department(self):
        rows = selectors.department_rows(
            self.filters(department_id=self.data.design.pk))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["department"], self.data.design)

    def test_department_rows_empty_for_an_unknown_department(self):
        self.assertEqual(
            selectors.department_rows(self.filters(department_id=999999)), [])

    # --- empty-set short circuits ---

    def test_employee_rows_empty_when_no_employee_matches(self):
        self.assertEqual(
            selectors.employee_rows(self.filters(employee_id=999999)), [])

    def test_task_without_a_completion_date_does_not_skew_cycle_time(self):
        """A DONE task whose completed_on was never written drops out of the
        completion window entirely (the query filters on that column), so the
        average is taken over the tasks that do have a date rather than
        counting the undated one as zero days."""
        from projects.models import Task

        Task.objects.filter(pk=self.data.done_a.pk).update(completed_on=None)
        rows = {r["profile"].user_id: r
                for r in selectors.employee_rows(self.filters())}
        anna = rows[self.data.anna.pk]
        self.assertEqual(anna["completed"].get("completed"), 1)
        self.assertIsNotNone(anna["cycle_days"])
