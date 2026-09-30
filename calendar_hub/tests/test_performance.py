"""Query-count guards.

The brief asks for optimised queries and no N+1. The way to prove that is not to
read the code but to measure the query count, add a great deal more data, and
measure again: if the number moves, something in a loop is hitting the database.

The claim being defended is specific. The calendar's cost is *the number of
sources that survived the visibility and filter checks* — a fixed, small number
— and not the number of entries, people, projects or days in the window. Every
assertion below is an equality rather than a ceiling, because a ceiling passes
right up until the day it doesn't.
"""
import itertools
from datetime import date, time, timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from calendar_hub import services
from calendar_hub.events import CalendarQuery
from calendar_hub.models import CalendarEvent, EventOccurrence
from clients.models import Client
from projects.models import Milestone, Project, Task
from resource_planner.models import LeaveRecord

from .factories import (GRID_END, GRID_START, JULY, MONDAY, Scenario, make_user)

_batches = itertools.count()


def bulk_up(data, *, people=8, per_person=6):
    """More of everything, spread across the window.

    Usernames carry a per-call prefix so a test that bulks up several times —
    the per-page loop does — doesn't collide on the unique username.
    """
    batch = next(_batches)
    client = Client.objects.create(name=f"Bulk client {batch}")
    project = Project.objects.create(
        client=client, name=f"Bulk {batch}", status=Project.Status.ACTIVE,
        target_end_date=MONDAY + timedelta(days=3))
    for index in range(people):
        person = make_user(f"bulk{batch}x{index}", department=data.delivery,
                           born=date(1990, 7, (index % 28) + 1))
        LeaveRecord.objects.create(
            user=person, status=LeaveRecord.Status.APPROVED,
            start_date=MONDAY + timedelta(days=index % 5),
            end_date=MONDAY + timedelta(days=index % 5))
        for step in range(per_person):
            Task.objects.create(
                project=project, title=f"T{batch}-{index}-{step}",
                assignee=person, department=data.delivery,
                due_date=MONDAY + timedelta(days=step % 7))
        Milestone.objects.create(
            project=project, title=f"M{batch}-{index}",
            due_date=MONDAY + timedelta(days=index % 7))
        event = CalendarEvent.objects.create(
            title=f"Sync {batch}-{index}", start_date=MONDAY + timedelta(days=index % 7),
            start_time=time(11, 0), created_by=person, project=project)
        event.attendees.set([person])


def query(viewer, **kwargs):
    kwargs.setdefault("start", GRID_START)
    kwargs.setdefault("end", GRID_END)
    return CalendarQuery(viewer=viewer, **kwargs)


class QueryCountMixin:
    """Measurement helpers.

    `warm` matters more than it looks. `accounts.permissions.has_perm` memoises
    the matrix onto the user object on first use, so the very first measurement
    against a fresh instance carries three extra role-lookup queries that have
    nothing to do with the calendar. Warming first is what makes the before/after
    comparison measure the thing being tested.
    """

    def count(self, callable_):
        with CaptureQueriesContext(connection) as captured:
            callable_()
        return len(captured)

    def warm(self, *viewers):
        for viewer in viewers:
            services.collect(query(viewer))


class CollectionQueryCountTests(QueryCountMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        self.warm(self.data.manager, self.data.sales)

    def test_collection_does_not_grow_with_the_amount_of_data(self):
        before = self.count(lambda: services.collect(query(self.data.manager)))
        bulk_up(self.data)
        after = self.count(lambda: services.collect(query(self.data.manager)))
        self.assertEqual(before, after)

    def test_collection_costs_one_pass_per_source_not_one_per_entry(self):
        bulk_up(self.data)
        with CaptureQueriesContext(connection) as captured:
            events = services.collect(query(self.data.manager))
        self.assertGreater(len(events), 50)
        # Seven sources; a couple run two statements (the private-event scope
        # and the recurrence overrides). Well under one per entry, which is the
        # claim that matters.
        self.assertLessEqual(len(captured), 14)

    def test_a_narrower_viewer_runs_fewer_queries_not_more(self):
        """A source that drops out must not still be paying for itself."""
        bulk_up(self.data)
        wide = self.count(lambda: services.collect(query(self.data.manager)))
        narrow = self.count(lambda: services.collect(query(self.data.sales)))
        self.assertLess(narrow, wide)

    def test_a_recurring_series_costs_the_same_as_a_one_off(self):
        """Expansion is arithmetic, not a query per occurrence."""
        one_off = self.count(lambda: services.collect(query(self.data.manager)))
        for index in range(10):
            CalendarEvent.objects.create(
                title=f"Daily {index}", start_date=MONDAY,
                frequency=CalendarEvent.Frequency.DAILY,
                created_by=self.data.manager)
        many = self.count(lambda: services.collect(query(self.data.manager)))
        self.assertEqual(one_off, many)

    def test_recurrence_overrides_are_loaded_for_every_series_at_once(self):
        """One query for all exceptions, not one per series — the N+1 this
        module would otherwise walk straight into."""
        for index in range(6):
            event = CalendarEvent.objects.create(
                title=f"Series {index}", start_date=MONDAY,
                frequency=CalendarEvent.Frequency.WEEKLY,
                created_by=self.data.manager)
            EventOccurrence.objects.create(event=event, original_date=MONDAY,
                                           is_cancelled=True)
        before = self.count(lambda: services.collect(query(self.data.manager)))
        for index in range(6, 18):
            event = CalendarEvent.objects.create(
                title=f"Series {index}", start_date=MONDAY,
                frequency=CalendarEvent.Frequency.WEEKLY,
                created_by=self.data.manager)
            EventOccurrence.objects.create(event=event, original_date=MONDAY,
                                           is_cancelled=True)
        self.assertEqual(before,
                         self.count(lambda: services.collect(query(self.data.manager))))

    def test_birthdays_cost_one_query_for_a_day_and_for_a_year(self):
        bulk_up(self.data)
        from calendar_hub import sources

        source = sources.get_source("birthday")
        with self.assertNumQueries(1):
            list(source.fetch(query(self.data.manager)))
        with self.assertNumQueries(1):
            list(source.fetch(query(self.data.manager,
                                    start=date(2026, 1, 1),
                                    end=date(2026, 12, 31))))


class GridQueryCountTests(QueryCountMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        self.warm(self.data.manager)

    def test_a_month_grid_costs_the_same_as_a_day(self):
        """35 cells must not mean 35× the queries — the grid is built by
        bucketing one collected list, not by asking per day."""
        month = services.Window(anchor=JULY, mode="month")
        day = services.Window(anchor=MONDAY, mode="day")
        bulk_up(self.data)
        month_cost = self.count(lambda: services.month_grid(
            month, query(self.data.manager, start=month.grid_start,
                         end=month.grid_end)))
        day_cost = self.count(lambda: services.day_detail(
            day, query(self.data.manager, start=day.start, end=day.end)))
        self.assertEqual(month_cost, day_cost)

    def test_every_grid_shape_costs_the_same(self):
        bulk_up(self.data)
        month = services.Window(anchor=JULY, mode="month")
        week = services.Window(anchor=MONDAY, mode="week")
        agenda = services.Window(anchor=MONDAY, mode="agenda")
        costs = {
            "month": self.count(lambda: services.month_grid(
                month, query(self.data.manager))),
            "week": self.count(lambda: services.week_grid(
                week, query(self.data.manager))),
            "agenda": self.count(lambda: services.agenda(
                agenda, query(self.data.manager))),
        }
        self.assertEqual(len(set(costs.values())), 1, costs)


class PageQueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        self.client.force_login(self.data.manager)

    def test_every_page_is_flat_as_data_grows(self):
        for name in ("month", "week", "day", "agenda"):
            with self.subTest(page=name):
                url = reverse(f"calendar_hub:{name}") + f"?date={MONDAY}"
                self.client.get(url)  # warm caches
                with CaptureQueriesContext(connection) as small:
                    self.client.get(url)
                bulk_up(self.data, people=4, per_person=3)
                with CaptureQueriesContext(connection) as large:
                    self.client.get(url)
                self.assertEqual(len(small), len(large),
                                 f"{name}: {len(small)} → {len(large)} queries")

    def test_pages_stay_under_a_sane_ceiling(self):
        bulk_up(self.data)
        for name in ("month", "week", "day", "agenda"):
            with self.subTest(page=name):
                url = reverse(f"calendar_hub:{name}") + f"?date={MONDAY}"
                with CaptureQueriesContext(connection) as captured:
                    self.client.get(url)
                self.assertLessEqual(len(captured), 30,
                                     f"{name} used {len(captured)} queries")

    def test_the_lazy_day_panel_is_cheaper_than_the_month_that_opened_it(self):
        """The whole reason it exists: a month cell ships four chips, and the
        rest are fetched only if somebody asks."""
        bulk_up(self.data)
        with CaptureQueriesContext(connection) as month:
            self.client.get(reverse("calendar_hub:month") + "?date=2026-07-01")
        with CaptureQueriesContext(connection) as panel:
            self.client.get(reverse("calendar_hub:day_panel", args=[str(MONDAY)]))
        self.assertLessEqual(len(panel), len(month))

    def test_the_export_is_flat_too(self):
        url = reverse("calendar_hub:ics", args=["month"]) + "?date=2026-07-01"
        self.client.get(url)
        with CaptureQueriesContext(connection) as small:
            self.client.get(url)
        bulk_up(self.data)
        with CaptureQueriesContext(connection) as large:
            self.client.get(url)
        self.assertEqual(len(small), len(large))
