"""Metric tests: the composite scores behave, and no metric contradicts the
model property it mirrors."""
from datetime import timedelta
from decimal import Decimal

from django.test import RequestFactory, TestCase
from django.utils import timezone

from analytics import selectors, services

from .factories import Scenario


def default_filters():
    return selectors.Filters.from_request(RequestFactory().get("/analytics/"))


class HelperTests(TestCase):
    def test_working_days_skips_weekends(self):
        # Mon 6 Jul 2026 → Sun 12 Jul 2026 is one full week: 5 working days.
        from datetime import date
        self.assertEqual(services.working_days(date(2026, 7, 6), date(2026, 7, 12)), 5)

    def test_working_days_single_saturday_is_zero(self):
        from datetime import date
        self.assertEqual(services.working_days(date(2026, 7, 11), date(2026, 7, 11)), 0)

    def test_percent_of_zero_is_zero_not_a_crash(self):
        self.assertEqual(services._percent(5, 0), 0.0)

    def test_percent_handles_none(self):
        self.assertEqual(services._percent(None, None), 0.0)

    def test_percent_rounds_to_one_decimal(self):
        self.assertEqual(services._percent(1, 3), 33.3)


class OverviewCardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def cards(self, **kwargs):
        return {c["label"]: c["value"]
                for c in services.overview_cards(default_filters(), **kwargs)}

    def test_headline_counts(self):
        cards = self.cards()
        self.assertEqual(cards["Clients"], 2)
        self.assertEqual(cards["Active projects"], 2)
        self.assertEqual(cards["Employees"], 4)
        self.assertEqual(cards["Tasks due today"], 1)
        self.assertEqual(cards["Tasks completed"], 3)
        self.assertEqual(cards["Billable hours"], Scenario.TEAM_BILLABLE)
        self.assertEqual(cards["Pending approvals"], 1)

    def test_finance_cards_present_for_finance_users(self):
        cards = self.cards()
        self.assertEqual(cards["Revenue"], Scenario.WEBSITE_RECEIVED)
        self.assertEqual(cards["Outstanding"], Scenario.PORTFOLIO_OUTSTANDING)

    def test_finance_cards_absent_without_the_permission(self):
        """Omitted, not zeroed — a ₹0 revenue card still discloses that revenue
        is a thing this person can be told about."""
        cards = self.cards(include_finance=False)
        self.assertNotIn("Revenue", cards)
        self.assertNotIn("Outstanding", cards)
        self.assertIn("Billable hours", cards)

    def test_overpayment_is_relabelled_as_credit(self):
        """Mirrors core.views.dashboard: "₹-25,000 outstanding" reads as a bug."""
        from finance.models import Payment
        Payment.objects.create(project=self.data.branding,
                               amount=Decimal("500000"),
                               received_on=self.data.today)
        labels = [c["label"] for c in services.overview_cards(default_filters())]
        self.assertIn("Client credit", labels)
        self.assertNotIn("Outstanding", labels)


class EmployeeMetricTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def rows(self):
        return {r["user"].username: r
                for r in services.employee_metrics(default_filters())}

    def test_totals_and_billable_share(self):
        anna = self.rows()["anna"]
        self.assertEqual(anna["total_hours"], Decimal("18.00"))
        # 12 of 18 billable.
        self.assertAlmostEqual(anna["billable_percent"], 66.7, places=1)

    def test_daily_average_uses_days_worked_not_window_length(self):
        """Anna logged 18 h across 3 days: 6 h/day, not 18/90."""
        self.assertEqual(self.rows()["anna"]["avg_daily_hours"], Decimal("6.00"))

    def test_approval_percent_ignores_drafts(self):
        """12 of Anna's 18 reviewed hours are approved; nothing is a draft."""
        self.assertAlmostEqual(self.rows()["anna"]["approval_percent"], 66.7,
                               places=1)

    def test_overdue_count_matches_the_task_property(self):
        anna_row = self.rows()["anna"]
        from projects.models import Task
        expected = sum(1 for t in Task.objects.filter(assignee=self.data.anna)
                       if t.is_overdue)
        self.assertEqual(anna_row["overdue_tasks"], expected)

    def test_completion_time_is_reported_in_days(self):
        anna = self.rows()["anna"]
        self.assertIsNotNone(anna["avg_completion_days"])
        self.assertGreaterEqual(anna["avg_completion_days"], 0)

    def test_person_with_no_logs_reports_zero_not_none(self):
        outsider = self.rows()["sales"]
        self.assertEqual(outsider["total_hours"], Decimal("0.00"))
        self.assertIsNone(outsider["avg_daily_hours"])
        self.assertEqual(outsider["utilization_percent"], 0.0)

    def test_rows_are_sorted_by_hours_descending(self):
        rows = services.employee_metrics(default_filters())
        hours = [r["total_hours"] for r in rows]
        self.assertEqual(hours, sorted(hours, reverse=True))

    def test_utilization_is_not_capped_at_100(self):
        """Overtime is the finding; capping would hide it."""
        metric = {"total_hours": Decimal("100")}
        self.assertGreater(services._percent(Decimal("100"), Decimal("40")), 100)


class ProjectMetricTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def rows(self, **kwargs):
        return {r["project"].name: r
                for r in services.project_metrics(default_filters(), **kwargs)}

    def test_effort_and_variance(self):
        website = self.rows()["Website"]
        # Estimates 5 + 3 + 6 = 14 (the due-today task has none); actual 18.
        self.assertEqual(website["estimated_hours"], Decimal("14.00"))
        self.assertEqual(website["actual_hours"], Decimal("18.00"))
        self.assertEqual(website["hours_variance"], Decimal("4.00"))

    def test_completion_and_milestone_progress(self):
        website = self.rows()["Website"]
        self.assertEqual(website["completion_percent"], 50.0)   # 2 of 4 tasks
        self.assertEqual(website["milestone_progress"], 50.0)   # 1 of 2

    def test_outstanding_matches_the_model_property(self):
        """Analytics and the project page must never quote different money."""
        website = self.rows()["Website"]
        self.assertEqual(website["outstanding"],
                         Decimal(self.data.website.outstanding).quantize(
                             Decimal("0.01")))

    def test_late_over_estimate_project_is_high_risk(self):
        website = self.rows()["Website"]
        self.assertEqual(website["risk"]["level"], "HIGH")
        self.assertIn("past target date", website["risk"]["reasons"])

    def test_branding_is_flagged_over_estimate_and_under_collected(self):
        """4 h estimated against 6 h logged, and delivered without a rupee
        received — two independent signals, so HIGH."""
        branding = self.rows()["Branding"]
        self.assertEqual(branding["risk"]["level"], "HIGH")
        self.assertIn("over effort estimate", branding["risk"]["reasons"])
        self.assertIn("delivery ahead of collection", branding["risk"]["reasons"])

    def test_a_project_with_nothing_wrong_is_low_risk(self):
        from projects.models import Project
        Project.objects.create(client=self.data.client_a, name="Clean",
                               status=Project.Status.ACTIVE,
                               target_end_date=self.data.today + timedelta(days=60))
        clean = self.rows()["Clean"]
        self.assertEqual(clean["risk"]["level"], "LOW")
        self.assertEqual(clean["risk"]["reasons"], [])
        self.assertEqual(clean["health"], 100)

    def test_health_is_bounded_to_0_100(self):
        for row in services.project_metrics(default_filters()):
            self.assertGreaterEqual(row["health"], 0)
            self.assertLessEqual(row["health"], 100)

    def test_health_deducts_for_a_missed_target_date(self):
        website = self.rows()["Website"]
        self.assertLess(website["health"], 100)

    def test_rows_sorted_worst_health_first(self):
        rows = services.project_metrics(default_filters())
        scores = [r["health"] for r in rows]
        self.assertEqual(scores, sorted(scores))

    def test_finance_columns_absent_without_permission(self):
        website = self.rows(include_finance=False)["Website"]
        self.assertNotIn("budget", website)
        self.assertNotIn("outstanding", website)
        self.assertIn("completion_percent", website)

    def test_finance_blind_health_is_never_lower_than_finance_aware(self):
        """The collection deduction is invisible without finance.view, so that
        reader sees an optimistic score — never a pessimistic one they'd have
        no way to explain."""
        with_money = self.rows()["Branding"]["health"]
        without = self.rows(include_finance=False)["Branding"]["health"]
        self.assertGreaterEqual(without, with_money)

    def test_finance_blind_health_leaks_no_money_signal(self):
        """Two projects identical on delivery but different on collection must
        score the same for someone who can't see money — otherwise the score
        is a side channel."""
        from projects.models import Project
        paid = Project.objects.create(client=self.data.client_a, name="Paid",
                                      status=Project.Status.ACTIVE,
                                      budget=Decimal("1000"))
        unpaid = Project.objects.create(client=self.data.client_a, name="Unpaid",
                                        status=Project.Status.ACTIVE,
                                        budget=Decimal("1000"))
        from finance.models import Payment
        Payment.objects.create(project=paid, amount=Decimal("1000"),
                               received_on=self.data.today)
        rows = self.rows(include_finance=False)
        self.assertEqual(rows["Paid"]["health"], rows["Unpaid"]["health"])

    def test_unestimated_project_is_not_penalised(self):
        """No estimate means unmeasured, not over-budget."""
        from projects.models import Project, Task
        project = Project.objects.create(client=self.data.client_a,
                                         name="Fresh", status=Project.Status.ACTIVE)
        Task.objects.create(project=project, title="t")
        row = self.rows()["Fresh"]
        self.assertEqual(row["health"], 100)
        self.assertEqual(row["risk"]["level"], "LOW")


class FinanceMetricTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_revenue_and_averages(self):
        metrics = services.finance_metrics(default_filters())
        self.assertEqual(metrics["revenue"], Decimal("40000.00"))
        self.assertEqual(metrics["portfolio_value"], Decimal("150000.00"))
        self.assertEqual(metrics["outstanding"], Decimal("110000.00"))
        # 150000 across 4 live projects — the two real ones plus the zero-budget
        # default project each client is given on creation.
        self.assertEqual(metrics["avg_project_value"], Decimal("37500.00"))
        # 150000 over 2 live clients.
        self.assertEqual(metrics["avg_client_value"], Decimal("75000.00"))

    def test_collection_percent(self):
        metrics = services.finance_metrics(default_filters())
        self.assertAlmostEqual(metrics["collection_percent"], 26.7, places=1)

    def test_empty_range_returns_zeroes_not_division_errors(self):
        filters = default_filters()
        filters.start = self.data.today + timedelta(days=30)
        filters.end = self.data.today + timedelta(days=60)
        metrics = services.finance_metrics(filters)
        self.assertEqual(metrics["revenue"], Decimal("0.00"))
        self.assertEqual(metrics["avg_payment"], Decimal("0"))


class ClientMetricTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def rows(self, **kwargs):
        return {r["client"].name: r
                for r in services.client_metrics(default_filters(), **kwargs)}

    def test_client_totals(self):
        acme = self.rows()["Acme"]
        # "Website" plus the default project clients/signals.py creates; the
        # archived one is excluded.
        self.assertEqual(acme["project_count"], 2)
        self.assertEqual(acme["revenue"], Decimal("40000.00"))
        self.assertEqual(acme["hours"], Decimal("18.00"))
        self.assertEqual(acme["documents"], 1)

    def test_health_is_bounded(self):
        for row in services.client_metrics(default_filters()):
            self.assertGreaterEqual(row["health"], 0)
            self.assertLessEqual(row["health"], 100)

    def test_finance_columns_absent_without_permission(self):
        acme = self.rows(include_finance=False)["Acme"]
        self.assertNotIn("revenue", acme)
        self.assertIn("hours", acme)

    def test_client_with_no_active_work_scores_lower(self):
        from clients.models import Client
        Client.objects.create(name="Dormant")
        rows = self.rows()
        self.assertLess(rows["Dormant"]["health"], rows["Acme"]["health"])


class DepartmentMetricTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_hours_and_headcount_per_department(self):
        rows = {r["department"].name: r
                for r in services.department_metrics(default_filters())}
        self.assertEqual(rows["Delivery"]["hours"], Decimal("18.00"))
        self.assertEqual(rows["Design"]["hours"], Decimal("6.00"))
        self.assertEqual(rows["Delivery"]["headcount"], 2)

    def test_utilization_scales_with_headcount(self):
        """Two people logging 18 h is half the utilisation of one person doing
        the same — otherwise a big team always looks busier."""
        rows = {r["department"].name: r
                for r in services.department_metrics(default_filters())}
        self.assertLess(rows["Delivery"]["utilization_percent"],
                        rows["Design"]["utilization_percent"] * 2)
