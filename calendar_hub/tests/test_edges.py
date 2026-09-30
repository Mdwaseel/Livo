"""Edge paths: empty calendars, boundary dates, and the routes a normal click
never takes.

These get their own file rather than cluttering the behavioural suites — a
hand-typed URL, a role edited mid-session, a month with nothing in it at all.
"""
from datetime import date, time

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from calendar_hub import selectors, services, sources
from calendar_hub.events import KINDS, CalendarQuery, Event
from calendar_hub.models import CalendarEvent
from calendar_hub.permissions import can_manage
from documents.models import Document
from projects.models import Task

from .factories import (FRIDAY, GRID_END, GRID_START, JULY, MONDAY, THURSDAY,
                        TUESDAY, Scenario, a_private_event, forget_perms,
                        make_user, set_perm)


class EmptyCalendarTests(TestCase):
    """A brand-new install, where nothing has been created yet."""

    def setUp(self):
        self.user = make_user("solo", role="Manager")
        self.client.force_login(self.user)

    def test_every_page_renders_with_no_data_at_all(self):
        for name in ("month", "week", "day", "agenda"):
            with self.subTest(page=name):
                response = self.client.get(reverse(f"calendar_hub:{name}"))
                self.assertEqual(response.status_code, 200)

    def test_the_month_grid_still_has_its_rows(self):
        """An empty month is a grid of empty cells, not an absent grid."""
        response = self.client.get(reverse("calendar_hub:month") + "?date=2026-07-01")
        self.assertEqual(len(response.context["weeks"]), 5)
        self.assertEqual(response.context["total"], 0)

    def test_the_agenda_says_so_rather_than_showing_a_blank_card(self):
        response = self.client.get(reverse("calendar_hub:agenda"))
        self.assertContains(response, "Nothing in this fortnight")

    def test_the_legend_still_lists_every_kind_with_a_zero(self):
        response = self.client.get(reverse("calendar_hub:month"))
        counts = {row["kind"].key: row["count"]
                  for row in response.context["kind_counts"]}
        self.assertEqual(set(counts), set(KINDS))
        self.assertTrue(all(value == 0 for value in counts.values()))

    def test_an_empty_export_is_still_a_valid_file(self):
        response = self.client.get(reverse("calendar_hub:ics", args=["month"]))
        body = response.content.decode("utf-8")
        self.assertIn("BEGIN:VCALENDAR", body)
        self.assertNotIn("BEGIN:VEVENT", body)


class BoundaryDateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_an_entry_on_the_first_of_the_month_is_in_the_grid(self):
        Task.objects.create(project=self.data.project, title="First",
                            assignee=self.data.anna, due_date=JULY)
        self.client.force_login(self.data.manager)
        response = self.client.get(reverse("calendar_hub:month") + "?date=2026-07-15")
        self.assertContains(response, "First")

    def test_an_entry_on_the_last_of_the_month_is_in_the_grid(self):
        Task.objects.create(project=self.data.project, title="Last",
                            assignee=self.data.anna, due_date=date(2026, 7, 31))
        self.client.force_login(self.data.manager)
        response = self.client.get(reverse("calendar_hub:month") + "?date=2026-07-01")
        self.assertContains(response, "Last")

    def test_a_february_month_grid_is_built_correctly(self):
        """February 2026 starts on a Sunday and has 28 days — the month most
        likely to expose an off-by-one in the grid."""
        window = services.Window(anchor=date(2026, 2, 10), mode="month")
        self.assertEqual(window.start, date(2026, 2, 1))
        self.assertEqual(window.end, date(2026, 2, 28))
        grid = services.month_grid(
            window, CalendarQuery(start=window.grid_start, end=window.grid_end,
                                  viewer=self.data.manager))
        self.assertEqual(sum(len(week) for week in grid["weeks"]) % 7, 0)

    def test_a_leap_february_ends_on_the_29th(self):
        window = services.Window(anchor=date(2028, 2, 10), mode="month")
        self.assertEqual(window.end, date(2028, 2, 29))

    def test_a_december_month_steps_into_january(self):
        window = services.Window(anchor=date(2026, 12, 15), mode="month")
        self.assertEqual(window.next, date(2027, 1, 1))


class SelectorEdgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_visible_events_for_an_anonymous_caller_shows_only_agency_ones(self):
        """The unauthenticated path is the one a future portal will hit first."""
        a_private_event(self.data.pm)
        titles = set(selectors.visible_events(None).values_list("title", flat=True))
        self.assertNotIn("One to one", titles)
        self.assertIn("Client review", titles)

    def test_already_reminded_with_no_keys_is_an_empty_set_not_a_query(self):
        with self.assertNumQueries(0):
            self.assertEqual(selectors.already_reminded([], "REMINDER"), set())

    def test_the_filter_dropdowns_exclude_archived_records(self):
        self.data.other_project.is_archived = True
        self.data.other_project.save()
        options = selectors.filter_options()
        self.assertNotIn(self.data.other_project,
                         list(options["projects"]))

    def test_event_or_none_returns_none_for_something_hidden(self):
        event = a_private_event(self.data.pm)
        self.assertIsNone(selectors.event_or_none(event.pk, self.data.bilal))
        self.assertIsNotNone(selectors.event_or_none(event.pk, self.data.pm))


class SourceEdgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def query(self, viewer, **kwargs):
        kwargs.setdefault("start", GRID_START)
        kwargs.setdefault("end", GRID_END)
        return CalendarQuery(viewer=viewer, **kwargs)

    def test_an_anonymous_viewer_gets_no_sources_at_all(self):
        self.assertEqual(sources.sources_for(self.query(None)), [])

    def test_a_read_only_source_refuses_a_hand_built_drag(self):
        """Leave is never draggable in the UI, so reaching this means somebody
        posted it directly — a refusal, not a traceback."""
        from django.core.exceptions import PermissionDenied

        with self.assertRaises(PermissionDenied):
            sources.get_source("leave").move(
                self.data.manager, self.data.leave.pk, FRIDAY)

    def test_moving_something_to_the_day_it_is_already_on_changes_nothing(self):
        source = sources.get_source("task")
        _, message = source.move(self.data.manager, self.data.task.pk, MONDAY)
        self.assertEqual(message, "")
        self.data.task.refresh_from_db()
        self.assertEqual(self.data.task.due_date, MONDAY)

    def test_a_task_with_no_assignee_still_moves(self):
        """The notification is skipped, not the move."""
        task = Task.objects.create(project=self.data.project, title="Orphan",
                                   due_date=MONDAY)
        sources.get_source("task").move(self.data.manager, task.pk, FRIDAY)
        task.refresh_from_db()
        self.assertEqual(task.due_date, FRIDAY)

    def test_a_document_whose_type_is_hidden_reports_as_missing_not_forbidden(self):
        from calendar_hub.sources.documents import DocumentReviewSource

        set_perm("Developer", "documents", "edit", True)
        forget_perms(self.data.anna)
        with self.assertRaises(LookupError):
            DocumentReviewSource().move(self.data.anna, self.data.invoice.pk,
                                        FRIDAY)

    def test_an_archived_document_is_not_on_the_calendar(self):
        self.data.document.is_archived = True
        self.data.document.save()
        titles = {event.title for event in sources.get_source("document").fetch(
            self.query(self.data.manager))}
        self.assertNotIn("Review: Website SRS", titles)
        # The other document in the same project is untouched, which is what
        # makes this about archiving rather than about the query breaking.
        self.assertIn("Review: Invoice INV-0042", titles)

    def test_a_settled_document_review_is_never_overdue(self):
        """A deadline on an approved document is history, not a task."""
        self.data.document.status = Document.Status.APPROVED
        self.data.document.review_due_date = date(2020, 1, 1)
        self.data.document.save()
        event = list(sources.get_source("document").fetch(
            self.query(self.data.manager, start=date(2020, 1, 1),
                       end=date(2020, 1, 2))))[0]
        self.assertFalse(event.is_overdue)

    def test_an_employee_with_no_birthday_recorded_is_simply_absent(self):
        self.data.anna.employee_profile.date_of_birth = None
        self.data.anna.employee_profile.save()
        self.assertEqual(
            list(sources.get_source("birthday").fetch(
                self.query(self.data.manager))), [])


class PermissionHelperTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_can_manage_is_false_for_anonymous(self):
        self.assertFalse(can_manage(None, self.data.meeting))

    def test_the_organiser_can_manage_without_the_edit_right(self):
        event = CalendarEvent.objects.create(title="Mine", start_date=MONDAY,
                                             created_by=self.data.anna)
        set_perm("Developer", "calendar", "edit", False)
        forget_perms(self.data.anna)
        self.assertTrue(can_manage(self.data.anna, event))
        self.assertFalse(can_manage(self.data.anna, self.data.meeting))

    def test_the_calendars_edit_right_governs_only_its_own_events(self):
        """`calendar.edit` must never be a way to reschedule a task, a
        milestone or a project deadline."""
        governed = {source.key for source in sources.all_sources()
                    if source.module == "calendar"}
        self.assertEqual(governed, {"event"})


class EventValueEdgeTests(SimpleTestCase):
    def test_a_span_clipped_entirely_outside_the_window_yields_nothing(self):
        event = Event(key="x", kind="leave", title="Away",
                      start_date=MONDAY, end_date=TUESDAY)
        self.assertEqual(list(event.dates_within(THURSDAY, FRIDAY)), [])

    def test_covers_is_inclusive_at_both_ends(self):
        event = Event(key="x", kind="leave", title="Away",
                      start_date=MONDAY, end_date=THURSDAY)
        self.assertTrue(event.covers(MONDAY))
        self.assertTrue(event.covers(THURSDAY))
        self.assertFalse(event.covers(FRIDAY))

    def test_at_returns_the_same_object_for_a_single_day_event(self):
        """No allocation for the common case."""
        event = Event(key="x", kind="task", title="T", start_date=MONDAY)
        self.assertIs(event.at(MONDAY), event)

    def test_a_query_can_be_narrowed_without_mutation(self):
        original = CalendarQuery(start=MONDAY, end=FRIDAY)
        narrowed = original.replace(end=TUESDAY)
        self.assertEqual(original.end, FRIDAY)
        self.assertEqual(narrowed.end, TUESDAY)

    def test_the_sort_key_orders_by_time_then_kind_then_title(self):
        early = Event(key="a", kind="meeting", title="Zebra",
                      start_date=MONDAY, start_time=time(9, 0))
        late = Event(key="b", kind="meeting", title="Apple",
                     start_date=MONDAY, start_time=time(17, 0))
        all_day = Event(key="c", kind="task", title="Aardvark",
                        start_date=MONDAY)
        ordered = sorted([all_day, late, early], key=lambda e: e.sort_key)
        self.assertEqual([e.key for e in ordered], ["a", "b", "c"])
