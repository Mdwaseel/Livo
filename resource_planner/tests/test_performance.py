"""Query-count guards.

The brief asks for heavy annotation use and no duplicate calculation. The way
to prove that is not to read the code but to measure the query count, add a lot
more data, and measure again: if the number moves, something in the loop is
hitting the database.

Ceilings are secondary and set above the measured cost — the flatness
assertions are the real test.
"""
import itertools
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from projects.models import Project, Task, WorkLogEntry
from resource_planner import selectors, services
from resource_planner.models import CapacityProfile, LeaveRecord

from .factories import MONDAY, SUNDAY, Scenario, make_user


def week():
    return services.Window(start=MONDAY, end=SUNDAY, mode="week")


_bulk_calls = itertools.count()


def bulk_up(data, *, people=10, tasks_each=5):
    """More people, more projects, more tasks, more logs, more leave.

    Usernames carry a per-call prefix so a test that bulks up several times —
    the per-page flatness loop does — doesn't collide on the unique username.
    """
    batch = next(_bulk_calls)
    project = Project.objects.create(
        client=data.client, name=f"Bulk {batch}", status=Project.Status.ACTIVE)
    for index in range(people):
        person = make_user(f"bulk{batch}x{index}", department=data.delivery)
        CapacityProfile.objects.create(user=person, daily_hours=Decimal("7"))
        LeaveRecord.objects.create(
            user=person, status=LeaveRecord.Status.APPROVED,
            start_date=MONDAY + timedelta(days=index % 5),
            end_date=MONDAY + timedelta(days=index % 5))
        for task_index in range(tasks_each):
            Task.objects.create(
                project=project, title=f"T{index}-{task_index}", assignee=person,
                department=data.delivery,
                due_date=MONDAY + timedelta(days=task_index % 7),
                estimated_hours=Decimal("3"))
            WorkLogEntry.objects.create(
                project=project, logged_by=person,
                date=MONDAY + timedelta(days=task_index % 7),
                description="w", hours=Decimal("1"))


class ServiceQueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def count(self, callable_):
        with CaptureQueriesContext(connection) as captured:
            callable_()
        return len(captured)

    def test_workload_query_count_does_not_grow_with_headcount(self):
        before = self.count(lambda: services.workload(week(), today=MONDAY))
        bulk_up(self.data)
        after = self.count(lambda: services.workload(week(), today=MONDAY))
        self.assertEqual(before, after)
        self.assertLessEqual(after, 12)

    def test_workload_does_not_touch_the_database_inside_its_loop(self):
        """Ten people, fifty tasks, fifty logs — same query count as three."""
        bulk_up(self.data)
        with CaptureQueriesContext(connection) as captured:
            rows = services.workload(week(), today=MONDAY)
        self.assertGreater(len(rows), 10)
        self.assertLessEqual(len(captured), 12)

    def test_planner_grid_is_flat_across_people_and_days(self):
        before = self.count(lambda: services.planner_grid(week(), today=MONDAY))
        bulk_up(self.data)
        after = self.count(lambda: services.planner_grid(week(), today=MONDAY))
        self.assertEqual(before, after)
        self.assertLessEqual(after, 10)

    def test_monthly_grid_costs_the_same_as_a_weekly_one(self):
        """31 columns must not mean 31× the queries — the grid is built from
        pre-grouped dictionaries, not per-day lookups."""
        bulk_up(self.data)
        weekly = self.count(
            lambda: services.planner_grid(week(), today=MONDAY))
        monthly = self.count(
            lambda: services.planner_grid(services.Window.for_month(MONDAY),
                                          today=MONDAY))
        self.assertEqual(weekly, monthly)

    def test_capacity_profiles_is_one_query_for_any_number_of_people(self):
        bulk_up(self.data)
        users = [profile.user for profile in selectors.plannable_users()]
        with self.assertNumQueries(1):
            selectors.capacity_profiles(users)

    def test_leave_calendar_is_built_from_one_query(self):
        bulk_up(self.data)
        with self.assertNumQueries(1):
            list(selectors.leave_records(MONDAY, SUNDAY))

    def test_department_workload_is_flat(self):
        before = self.count(
            lambda: services.department_workload(week(), today=MONDAY))
        bulk_up(self.data)
        self.assertEqual(before, self.count(
            lambda: services.department_workload(week(), today=MONDAY)))

    def test_project_allocation_is_flat_across_projects(self):
        before = self.count(
            lambda: selectors.project_allocation_rows(MONDAY, SUNDAY))
        bulk_up(self.data)
        for index in range(8):
            Project.objects.create(client=self.data.client, name=f"P{index}",
                                   status=Project.Status.ACTIVE)
        self.assertEqual(before, self.count(
            lambda: selectors.project_allocation_rows(MONDAY, SUNDAY)))

    def test_forecast_cost_scales_with_weeks_not_with_people(self):
        """One workload() per forecast week is by design — reusing the planner's
        arithmetic is worth the queries. What must not happen is the per-week
        cost growing with headcount."""
        small = self.count(lambda: services.capacity_forecast(weeks=4, today=MONDAY))
        bulk_up(self.data)
        large = self.count(lambda: services.capacity_forecast(weeks=4, today=MONDAY))
        self.assertEqual(small, large)


class PageQueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        self.client.force_login(self.data.manager)

    def test_every_page_is_flat_as_data_grows(self):
        for name in ("overview", "employees", "departments", "weekly", "monthly"):
            with self.subTest(page=name):
                url = reverse(f"resource_planner:{name}") + f"?date={MONDAY}"
                self.client.get(url)  # warm caches
                with CaptureQueriesContext(connection) as small:
                    self.client.get(url)
                bulk_up(self.data, people=4, tasks_each=3)
                with CaptureQueriesContext(connection) as large:
                    self.client.get(url)
                self.assertEqual(len(small), len(large),
                                 f"{name}: {len(small)} → {len(large)} queries")

    def test_pages_stay_under_a_sane_ceiling(self):
        bulk_up(self.data)
        # The overview is the expensive one: it runs an 8-week forecast, and
        # each week is a full workload() pass so the forecast can never disagree
        # with the planner. The other pages are single-window.
        # +2 on the single-window pages, both from the chrome rather than the
        # planner: one resolves which workspace the viewer is in, one counts the
        # leave requests waiting on them for the sidebar badge — the same
        # per-page cost the unread-notifications bell beside it already pays.
        # Both are per-REQUEST, not per-row. The property that actually matters
        # is guarded by `test_every_page_is_flat_as_data_grows` above, which is
        # unchanged: the count still does not move when the data quadruples.
        ceilings = {"overview": 110, "employees": 32, "departments": 32,
                    "projects": 32, "weekly": 32, "monthly": 32}
        for name, ceiling in ceilings.items():
            with self.subTest(page=name):
                url = reverse(f"resource_planner:{name}") + f"?date={MONDAY}"
                with CaptureQueriesContext(connection) as captured:
                    self.client.get(url)
                self.assertLessEqual(len(captured), ceiling,
                                     f"{name} used {len(captured)} queries")
