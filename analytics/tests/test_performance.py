"""Query-count guards.

`assertNumQueries` with an exact number would fail on every unrelated change to
the session or permission layer, so these assert a *ceiling* and — more
importantly — that the ceiling does not move when the data grows. A page whose
query count is flat from 3 rows to 40 has no N+1; one that climbs does, and no
amount of eyeballing `select_related` proves that as well as the second
assertion here.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse

from analytics import selectors, services
from clients.models import Client
from projects.models import Project, Task, WorkLogEntry

from .factories import Scenario


def bulk_up(data, *, clients=12):
    """Add a lot more rows of every kind the dashboards touch."""
    for index in range(clients):
        client = Client.objects.create(name=f"Client {index}")
        project = Project.objects.create(
            client=client, name=f"Project {index}",
            status=Project.Status.ACTIVE, budget=Decimal("10000"))
        for task_index in range(4):
            Task.objects.create(
                project=project, title=f"T{task_index}", assignee=data.anna,
                department=data.delivery, estimated_hours=Decimal("2"))
        for log_index in range(4):
            WorkLogEntry.objects.create(
                project=project, logged_by=data.anna,
                date=data.today - timedelta(days=log_index),
                description="w", hours=Decimal("1"), is_billable=True)


class SelectorQueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def filters(self):
        return selectors.Filters.from_request(RequestFactory().get("/analytics/"))

    def count(self, callable_):
        with CaptureQueriesContext(connection) as captured:
            callable_()
        return len(captured)

    def test_employee_rows_is_a_fixed_number_of_queries(self):
        filters = self.filters()
        before = self.count(lambda: selectors.employee_rows(filters))
        bulk_up(self.data)
        after = self.count(lambda: selectors.employee_rows(filters))
        self.assertEqual(before, after)
        self.assertLessEqual(after, 6)

    def test_project_rows_does_not_grow_with_project_count(self):
        filters = self.filters()
        before = self.count(lambda: selectors.project_rows(filters))
        bulk_up(self.data)
        after = self.count(lambda: selectors.project_rows(filters))
        self.assertEqual(before, after)
        self.assertLessEqual(after, 6)

    def test_client_rows_does_not_grow_with_client_count(self):
        filters = self.filters()
        before = self.count(lambda: selectors.client_rows(filters))
        bulk_up(self.data)
        after = self.count(lambda: selectors.client_rows(filters))
        self.assertEqual(before, after)
        self.assertLessEqual(after, 6)

    def test_department_rows_is_flat(self):
        filters = self.filters()
        before = self.count(lambda: selectors.department_rows(filters))
        bulk_up(self.data)
        self.assertEqual(before,
                         self.count(lambda: selectors.department_rows(filters)))

    def test_daily_series_is_one_query_regardless_of_range(self):
        filters = self.filters()
        self.assertEqual(self.count(lambda: selectors.daily_hours_series(filters)), 1)


class MetricQueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def filters(self):
        return selectors.Filters.from_request(RequestFactory().get("/analytics/"))

    def test_project_metrics_touches_no_model_properties(self):
        """`Project.outstanding` and `Task.is_overdue` each fire a query per
        row. The metric layer must read the annotations instead — this is the
        test that catches someone "simplifying" it back."""
        filters = self.filters()
        bulk_up(self.data)
        with CaptureQueriesContext(connection) as captured:
            rows = services.project_metrics(filters)
        self.assertGreater(len(rows), 12)
        self.assertLessEqual(len(captured), 6)

    def test_employee_metrics_does_not_query_per_person(self):
        filters = self.filters()
        with CaptureQueriesContext(connection) as captured:
            services.employee_metrics(filters)
        self.assertLessEqual(len(captured), 6)


class PageQueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_dashboard_query_count_is_flat_as_data_grows(self):
        self.client.force_login(self.data.manager)
        url = reverse("analytics:dashboard")
        self.client.get(url)  # warm any lazily-built caches first

        with CaptureQueriesContext(connection) as small:
            self.client.get(url)
        bulk_up(self.data)
        with CaptureQueriesContext(connection) as large:
            self.client.get(url)

        # 12 more clients, 12 projects, 48 tasks and 48 work logs must not cost
        # a single extra query.
        self.assertEqual(len(small), len(large))

    def test_each_page_stays_under_a_sane_ceiling(self):
        """A backstop, not the real N+1 guard — that is the flatness test above.

        The overview genuinely costs ~45: eight charts, four rollup tables and
        eleven cards, each aggregate answering a question none of the others
        does. The ceilings are set a little above the measured cost so a
        refactor that quietly doubles the work trips them, while an unrelated
        middleware change does not.
        """
        self.client.force_login(self.data.manager)
        bulk_up(self.data)
        ceilings = {"dashboard": 55, "employees": 35, "projects": 30,
                    "clients": 30, "finance": 30}
        for name, ceiling in ceilings.items():
            with self.subTest(page=name):
                with CaptureQueriesContext(connection) as captured:
                    self.client.get(reverse(f"analytics:{name}"))
                self.assertLessEqual(len(captured), ceiling,
                                     f"{name} used {len(captured)} queries")

    def test_export_is_flat_too(self):
        self.client.force_login(self.data.manager)
        url = reverse("analytics:export",
                      kwargs={"dataset": "projects", "fmt": "csv"})
        self.client.get(url)
        with CaptureQueriesContext(connection) as small:
            self.client.get(url)
        bulk_up(self.data)
        with CaptureQueriesContext(connection) as large:
            self.client.get(url)
        self.assertEqual(len(small), len(large))
