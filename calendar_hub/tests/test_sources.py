"""The seven source adapters, and the contract they all keep.

Two things are being checked throughout: that each source reads its own app
live rather than from anything stored here, and that the *filter* semantics are
the honest ones — a source that cannot answer a question returns nothing rather
than guessing.
"""
from datetime import date, timedelta

from django.test import TestCase

from calendar_hub import sources
from calendar_hub.events import CalendarQuery
from calendar_hub.models import CalendarEvent
from projects.models import Project, Task
from resource_planner.models import LeaveRecord

from .factories import (FRIDAY, GRID_END, GRID_START, MONDAY, THURSDAY,
                        TUESDAY, WEDNESDAY, Scenario, a_private_event)


def query(viewer, **kwargs):
    kwargs.setdefault("start", GRID_START)
    kwargs.setdefault("end", GRID_END)
    return CalendarQuery(viewer=viewer, **kwargs)


def fetch(key, viewer, **kwargs):
    return list(sources.get_source(key).fetch(query(viewer, **kwargs)))


class RegistryTests(TestCase):
    def test_every_source_declares_a_complete_contract(self):
        """A half-declared source is worse than a missing one: it registers,
        renders nothing, and looks like empty data."""
        for source in sources.all_sources():
            with self.subTest(source=source.key):
                self.assertTrue(source.key)
                self.assertTrue(source.kinds)
                self.assertTrue(source.module)
                self.assertIsInstance(source.supports, frozenset)

    def test_every_declared_kind_is_a_real_kind(self):
        from calendar_hub.events import KINDS

        for source in sources.all_sources():
            for kind in source.kinds:
                self.assertIn(kind, KINDS, f"{source.key} emits unknown {kind}")

    def test_registering_the_same_key_twice_is_refused(self):
        """Two sources under one key would make `Event.key` ambiguous, and the
        drag endpoint would route a move to whichever won."""
        with self.assertRaises(sources.base.ImproperlyRegistered):
            @sources.register
            class Duplicate(sources.EventSource):
                key = "task"


class SourceSelectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_source_drops_out_when_the_viewer_lacks_its_module(self):
        """Sales holds no `tasks` permission, so the task source never runs its
        query at all — the data isn't loaded and then filtered."""
        chosen = {s.key for s in sources.sources_for(query(self.data.sales))}
        self.assertNotIn("task", chosen)
        self.assertIn("event", chosen)

    def test_a_manager_gets_every_source(self):
        chosen = {s.key for s in sources.sources_for(query(self.data.manager))}
        self.assertEqual(chosen, {s.key for s in sources.all_sources()})

    def test_a_source_drops_out_when_a_filter_it_cannot_honour_is_set(self):
        """Filtering to one employee and still seeing every project milestone
        would answer a different question from the one asked."""
        chosen = {s.key for s in sources.sources_for(
            query(self.data.manager, employee_id=self.data.anna.pk))}
        self.assertNotIn("milestone", chosen)
        self.assertNotIn("project", chosen)
        self.assertIn("task", chosen)
        self.assertIn("birthday", chosen)

    def test_a_source_drops_out_when_its_kinds_were_not_asked_for(self):
        chosen = {s.key for s in sources.sources_for(
            query(self.data.manager, kinds=frozenset({"task"})))}
        self.assertEqual(chosen, {"task"})

    def test_asking_for_leave_keeps_the_source_that_also_emits_holidays(self):
        """A source is kept if *any* of its kinds was asked for, and filters the
        rest out itself."""
        chosen = {s.key for s in sources.sources_for(
            query(self.data.manager, kinds=frozenset({"holiday"})))}
        self.assertIn("leave", chosen)


class TaskSourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_task_appears_on_its_due_date(self):
        events = fetch("task", self.data.manager)
        found = {event.title: event for event in events}
        self.assertIn("Build header", found)
        self.assertEqual(found["Build header"].start_date, MONDAY)

    def test_a_task_carries_all_four_scope_dimensions(self):
        event = fetch("task", self.data.manager)[0]
        self.assertEqual(event.user_id, self.data.anna.pk)
        self.assertEqual(event.department_id, self.data.delivery.pk)
        self.assertEqual(event.project_id, self.data.project.pk)
        self.assertEqual(event.client_id, self.data.client.pk)

    def test_a_task_with_no_due_date_is_not_on_the_calendar(self):
        Task.objects.create(project=self.data.project, title="Someday",
                            assignee=self.data.anna)
        titles = {event.title for event in fetch("task", self.data.manager)}
        self.assertNotIn("Someday", titles)

    def test_an_unfinished_task_past_its_date_is_flagged_overdue(self):
        Task.objects.create(project=self.data.project, title="Late one",
                            assignee=self.data.anna,
                            due_date=date(2026, 7, 2))
        events = {event.title: event for event in fetch("task", self.data.manager)}
        # 2 July 2026 is in the past relative to nothing — overdue is computed
        # against the real today, so assert the flag tracks the comparison
        # rather than hardcoding an answer that expires.
        from django.utils import timezone
        expected = date(2026, 7, 2) < timezone.localdate()
        self.assertEqual(events["Late one"].is_overdue, expected)

    def test_a_completed_task_is_never_overdue(self):
        Task.objects.create(project=self.data.project, title="Finished",
                            assignee=self.data.anna, status=Task.Status.DONE,
                            due_date=date(2026, 7, 2), completed_on=MONDAY)
        events = {event.title: event for event in fetch("task", self.data.manager)}
        self.assertFalse(events["Finished"].is_overdue)

    def test_completed_tasks_stay_on_the_calendar(self):
        """"What was due that week" is a question people ask about the past. A
        month that empties out behind you is disorienting."""
        Task.objects.create(project=self.data.project, title="Finished",
                            assignee=self.data.anna, status=Task.Status.DONE,
                            due_date=MONDAY, completed_on=MONDAY)
        titles = {event.title for event in fetch("task", self.data.manager)}
        self.assertIn("Finished", titles)

    def test_search_matches_the_title_and_the_project_name(self):
        by_title = fetch("task", self.data.manager, search="header")
        self.assertEqual([e.title for e in by_title], ["Build header"])
        by_project = fetch("task", self.data.manager, search="Website")
        self.assertIn("Build header", {e.title for e in by_project})

    def test_search_is_case_insensitive(self):
        self.assertTrue(fetch("task", self.data.manager, search="HEADER"))


class MilestoneAndProjectTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_milestone_appears_on_its_due_date(self):
        events = fetch("milestone", self.data.manager)
        self.assertEqual([e.start_date for e in events], [WEDNESDAY])

    def test_a_project_deadline_appears_on_its_target_end_date(self):
        events = fetch("project", self.data.manager)
        self.assertEqual([(e.title, e.start_date) for e in events],
                         [("Website due", FRIDAY)])

    def test_a_cancelled_project_has_no_deadline(self):
        """A deadline is only a deadline while the work is live."""
        self.data.project.status = Project.Status.CANCELLED
        self.data.project.save()
        self.assertEqual(fetch("project", self.data.manager), [])

    def test_an_archived_project_has_no_deadline(self):
        self.data.project.is_archived = True
        self.data.project.save()
        self.assertEqual(fetch("project", self.data.manager), [])

    def test_a_completed_project_keeps_its_marker_but_is_never_overdue(self):
        self.data.project.status = Project.Status.COMPLETED
        self.data.project.save()
        events = fetch("project", self.data.manager)
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].is_overdue)


class DocumentSourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_review_deadline_appears_for_someone_who_can_see_the_type(self):
        titles = {e.title for e in fetch("document", self.data.manager)}
        self.assertIn("Review: Website SRS", titles)

    def test_a_financial_document_is_hidden_from_someone_without_finance(self):
        """A calendar that leaks "Invoice INV-0042 review due Friday" to
        somebody barred from the invoices module has defeated the gate rather
        than passed it."""
        titles = {e.title for e in fetch("document", self.data.anna)}
        self.assertIn("Review: Website SRS", titles)
        self.assertNotIn("Review: Invoice INV-0042", titles)

    def test_a_financial_document_is_visible_to_a_manager(self):
        titles = {e.title for e in fetch("document", self.data.manager)}
        self.assertIn("Review: Invoice INV-0042", titles)

    def test_a_document_with_no_review_deadline_is_absent(self):
        self.data.document.review_due_date = None
        self.data.document.save()
        titles = {e.title for e in fetch("document", self.data.manager)}
        self.assertNotIn("Review: Website SRS", titles)


class LeaveSourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_leave_spans_every_day_it_covers(self):
        event = [e for e in fetch("leave", self.data.manager)
                 if e.kind == "leave"][0]
        self.assertEqual(event.days, 3)
        self.assertTrue(event.is_multi_day)

    def test_a_company_wide_holiday_is_its_own_kind(self):
        kinds = {e.kind for e in fetch("leave", self.data.manager)}
        self.assertEqual(kinds, {"leave", "holiday"})

    def test_a_holiday_survives_an_employee_filter(self):
        """A public holiday applies to the person being filtered on as much as
        to anybody else — the same rule the resource planner follows."""
        events = fetch("leave", self.data.manager,
                       employee_id=self.data.anna.pk)
        kinds = {e.kind for e in events}
        self.assertIn("holiday", kinds)
        self.assertNotIn("leave", kinds)  # Anna has none; Bilal's is filtered out

    def test_rejected_leave_never_reaches_the_calendar(self):
        LeaveRecord.objects.create(
            user=self.data.anna, status=LeaveRecord.Status.REJECTED,
            start_date=MONDAY, end_date=MONDAY)
        for event in fetch("leave", self.data.manager):
            self.assertNotEqual(event.user_id, self.data.anna.pk)

    def test_requested_leave_is_shown_but_marked(self):
        """Shown because it's a warning about somebody's week; marked because it
        isn't a fact about it yet."""
        LeaveRecord.objects.create(
            user=self.data.anna, status=LeaveRecord.Status.REQUESTED,
            start_date=THURSDAY, end_date=THURSDAY)
        event = [e for e in fetch("leave", self.data.manager)
                 if e.user_id == self.data.anna.pk][0]
        self.assertEqual(event.status_label, "Requested")

    def test_leave_is_never_draggable(self):
        """A range has two ends, so a drag is ambiguous — did the holiday move
        or get a day longer?"""
        self.assertFalse(sources.get_source("leave").is_movable)


class BirthdaySourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_birthday_lands_on_the_day_and_month_in_this_years_calendar(self):
        events = fetch("birthday", self.data.manager)
        self.assertEqual([e.start_date for e in events], [date(2026, 7, 8)])

    def test_the_birth_year_never_leaves_the_source(self):
        """A date of birth is an age, and an age is HR-sensitive in a way a
        birthday is not."""
        event = fetch("birthday", self.data.manager)[0]
        self.assertNotIn("1992", event.title)
        self.assertNotIn("1992", event.detail)
        self.assertEqual(event.start_date.year, 2026)

    def test_somebody_who_left_has_no_birthday_at_work(self):
        from employees.models import EmployeeProfile

        profile = self.data.anna.employee_profile
        profile.status = EmployeeProfile.Status.RESIGNED
        profile.save()
        self.assertEqual(fetch("birthday", self.data.manager), [])

    def test_29_february_falls_back_to_the_28th(self):
        person = self.data.bilal.employee_profile
        person.date_of_birth = date(1996, 2, 29)
        person.save()
        events = list(sources.get_source("birthday").fetch(
            CalendarQuery(start=date(2027, 2, 1), end=date(2027, 2, 28),
                          viewer=self.data.manager)))
        self.assertEqual([e.start_date for e in events], [date(2027, 2, 28)])

    def test_a_year_long_window_produces_one_birthday_per_person(self):
        events = list(sources.get_source("birthday").fetch(
            CalendarQuery(start=date(2026, 1, 1), end=date(2026, 12, 31),
                          viewer=self.data.manager)))
        self.assertEqual(len(events), 1)


class CalendarEventSourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_timed_meeting_keeps_its_times(self):
        meeting = [e for e in fetch("event", self.data.manager)
                   if e.kind == "meeting"][0]
        self.assertEqual(meeting.start_date, TUESDAY)
        self.assertFalse(meeting.is_all_day)
        self.assertEqual(meeting.start_time.hour, 14)

    def test_a_recurring_series_expands_across_the_window(self):
        standups = [e for e in fetch("event", self.data.manager)
                    if e.title == "Weekly stand-up"]
        self.assertEqual([e.start_date for e in standups],
                         [MONDAY, MONDAY + timedelta(days=7),
                          MONDAY + timedelta(days=14),
                          MONDAY + timedelta(days=21)])

    def test_each_occurrence_gets_its_own_key(self):
        standups = [e for e in fetch("event", self.data.manager)
                    if e.title == "Weekly stand-up"]
        self.assertEqual(len({e.key for e in standups}), len(standups))

    def test_a_cancelled_event_disappears_entirely(self):
        self.data.meeting.is_cancelled = True
        self.data.meeting.save()
        titles = {e.title for e in fetch("event", self.data.manager)}
        self.assertNotIn("Client review", titles)

    def test_a_private_event_is_invisible_to_an_outsider(self):
        a_private_event(self.data.pm, attendees=[self.data.anna])
        for_anna = {e.title for e in fetch("event", self.data.anna)}
        for_bilal = {e.title for e in fetch("event", self.data.bilal)}
        self.assertIn("One to one", for_anna)
        self.assertNotIn("One to one", for_bilal)

    def test_a_private_event_is_visible_to_its_organiser(self):
        a_private_event(self.data.pm)
        self.assertIn("One to one",
                      {e.title for e in fetch("event", self.data.pm)})

    def test_the_employee_filter_matches_attendees_and_the_organiser(self):
        for_anna = {e.title for e in fetch("event", self.data.manager,
                                           employee_id=self.data.anna.pk)}
        self.assertIn("Client review", for_anna)   # attendee
        for_manager = {e.title for e in fetch("event", self.data.manager,
                                              employee_id=self.data.manager.pk)}
        self.assertIn("Weekly stand-up", for_manager)  # organiser

    def test_the_client_is_filled_in_from_the_project(self):
        """Making the user say it twice is how the two end up disagreeing."""
        self.assertEqual(self.data.meeting.client_id, self.data.client.pk)

    def test_a_recurring_multi_day_event_keeps_its_span_each_time(self):
        offsite = CalendarEvent.objects.create(
            title="Offsite", start_date=MONDAY,
            end_date=MONDAY + timedelta(days=2),
            frequency=CalendarEvent.Frequency.MONTHLY,
            created_by=self.data.manager)
        found = [e for e in fetch("event", self.data.manager)
                 if e.object_id == offsite.pk]
        self.assertTrue(found)
        self.assertEqual(found[0].days, 3)
