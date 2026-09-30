"""The three tables: validation, derived values, and the rule that earned each
of them a place.
"""
from datetime import date, time, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from calendar_hub.models import (CalendarEvent, EventOccurrence, ReminderLog,
                                 WEEKDAY_LABELS)

from .factories import MONDAY, THURSDAY, TUESDAY, Scenario, make_user


class NoDuplicationTests(TestCase):
    """The module's own rule: store only what nothing else can answer."""

    def test_the_calendar_owns_exactly_three_tables(self):
        from django.apps import apps

        models = {model.__name__ for model in
                  apps.get_app_config("calendar_hub").get_models()}
        self.assertEqual(models,
                         {"CalendarEvent", "EventOccurrence", "ReminderLog"})

    def test_nothing_here_caches_a_date_another_app_owns(self):
        """Task due dates, milestones, deadlines, leave, birthdays and document
        reviews are read live. If any of them ever appears as a column here,
        this fails and somebody has to justify it."""
        forbidden = {"task", "task_id", "milestone", "milestone_id",
                     "due_date", "leave", "leave_id", "birthday",
                     "employee", "employee_id", "document", "document_id"}
        for model in (CalendarEvent, EventOccurrence, ReminderLog):
            fields = {field.name for field in model._meta.get_fields()}
            self.assertEqual(fields & forbidden, set(),
                             f"{model.__name__} is caching somebody else's data")

    def test_ordinary_occurrences_of_a_series_are_never_written_down(self):
        """Only exceptions are stored. Materialising a daily stand-up would be
        260 rows a year for a line on a grid."""
        Scenario()
        self.assertEqual(EventOccurrence.objects.count(), 0)


class CalendarEventTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_missing_start_time_is_the_all_day_flag(self):
        """A separate boolean would be a second source of truth for the same
        fact, and would eventually disagree with the times beside it."""
        self.assertTrue(CalendarEvent(start_date=MONDAY).is_all_day)
        self.assertFalse(CalendarEvent(start_date=MONDAY,
                                       start_time=time(9, 0)).is_all_day)

    def test_end_before_start_is_rejected(self):
        event = CalendarEvent(title="x", start_date=THURSDAY, end_date=MONDAY)
        with self.assertRaises(ValidationError) as caught:
            event.full_clean()
        self.assertIn("end_date", caught.exception.message_dict)

    def test_an_end_time_without_a_start_time_is_rejected(self):
        event = CalendarEvent(title="x", start_date=MONDAY, end_time=time(10, 0))
        with self.assertRaises(ValidationError) as caught:
            event.full_clean()
        self.assertIn("end_time", caught.exception.message_dict)

    def test_an_end_time_before_the_start_time_on_one_day_is_rejected(self):
        event = CalendarEvent(title="x", start_date=MONDAY,
                              start_time=time(15, 0), end_time=time(14, 0))
        with self.assertRaises(ValidationError):
            event.full_clean()

    def test_a_multi_day_event_may_end_at_an_earlier_clock_time(self):
        """An offsite that runs 09:00 Monday to 17:00 Wednesday is normal; only
        a single-day event has to end after it starts."""
        event = CalendarEvent(title="Offsite", start_date=MONDAY,
                              end_date=MONDAY + timedelta(days=2),
                              start_time=time(15, 0), end_time=time(9, 0))
        event.full_clean()  # must not raise

    def test_weekdays_only_apply_to_a_weekly_repeat(self):
        event = CalendarEvent(title="x", start_date=MONDAY, weekdays="15",
                              frequency=CalendarEvent.Frequency.MONTHLY)
        with self.assertRaises(ValidationError) as caught:
            event.full_clean()
        self.assertIn("weekdays", caught.exception.message_dict)

    def test_weekdays_must_be_iso_digits(self):
        event = CalendarEvent(title="x", start_date=MONDAY, weekdays="xyz",
                              frequency=CalendarEvent.Frequency.WEEKLY)
        with self.assertRaises(ValidationError):
            event.full_clean()

    def test_a_series_cannot_end_before_it_starts(self):
        event = CalendarEvent(title="x", start_date=THURSDAY,
                              frequency=CalendarEvent.Frequency.WEEKLY,
                              repeat_until=MONDAY)
        with self.assertRaises(ValidationError) as caught:
            event.full_clean()
        self.assertIn("repeat_until", caught.exception.message_dict)

    def test_a_bare_weekly_series_uses_the_start_dates_weekday(self):
        event = CalendarEvent(start_date=MONDAY,
                              frequency=CalendarEvent.Frequency.WEEKLY)
        self.assertEqual(event.weekday_numbers, [MONDAY.isoweekday()])

    def test_the_recurrence_label_reads_as_a_sentence(self):
        event = CalendarEvent(start_date=MONDAY, interval=2, weekdays="14",
                              frequency=CalendarEvent.Frequency.WEEKLY,
                              repeat_until=date(2026, 12, 12))
        self.assertEqual(event.recurrence_label,
                         "Every 2 weeks on Mon, Thu until 12 Dec 2026")

    def test_a_non_recurring_event_has_no_recurrence_label(self):
        self.assertEqual(CalendarEvent(start_date=MONDAY).recurrence_label, "")

    def test_the_client_is_filled_in_from_the_project_on_save(self):
        event = CalendarEvent.objects.create(
            title="Kickoff", start_date=MONDAY, project=self.data.project)
        self.assertEqual(event.client_id, self.data.client.pk)

    def test_an_explicit_client_is_not_overwritten(self):
        event = CalendarEvent.objects.create(
            title="Pitch", start_date=MONDAY,
            client=self.data.other_client, project=self.data.project)
        self.assertEqual(event.client_id, self.data.other_client.pk)

    def test_private_visibility_covers_the_organiser_and_the_invitees(self):
        event = CalendarEvent.objects.create(
            title="1:1", start_date=MONDAY, created_by=self.data.pm,
            visibility=CalendarEvent.Visibility.PRIVATE)
        event.attendees.set([self.data.anna])
        self.assertTrue(event.visible_to(self.data.pm))
        self.assertTrue(event.visible_to(self.data.anna))
        self.assertFalse(event.visible_to(self.data.bilal))

    def test_an_agency_event_is_visible_to_everyone(self):
        self.assertTrue(self.data.meeting.visible_to(self.data.bilal))

    def test_an_external_event_cannot_be_imported_twice(self):
        """The single most common way a calendar sync goes wrong."""
        CalendarEvent.objects.create(title="Remote", start_date=MONDAY,
                                     external_provider="google",
                                     external_id="abc123")
        with transaction.atomic(), self.assertRaises(IntegrityError):
            CalendarEvent.objects.create(title="Remote again", start_date=TUESDAY,
                                         external_provider="google",
                                         external_id="abc123")

    def test_local_events_are_exempt_from_the_external_constraint(self):
        """They all have a blank external_id; a plain unique constraint would
        allow exactly one local event to exist."""
        CalendarEvent.objects.create(title="A", start_date=MONDAY)
        CalendarEvent.objects.create(title="B", start_date=MONDAY)  # no error

    def test_weekday_labels_cover_all_seven_iso_days(self):
        self.assertEqual(set(WEEKDAY_LABELS), {1, 2, 3, 4, 5, 6, 7})


class EventOccurrenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_one_exception_per_instance(self):
        EventOccurrence.objects.create(event=self.data.standup,
                                       original_date=MONDAY)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            EventOccurrence.objects.create(event=self.data.standup,
                                           original_date=MONDAY)

    def test_an_unmoved_exception_reports_its_original_date(self):
        override = EventOccurrence(event=self.data.standup,
                                   original_date=MONDAY)
        self.assertEqual(override.effective_date, MONDAY)

    def test_a_moved_exception_reports_where_it_went(self):
        override = EventOccurrence(event=self.data.standup,
                                   original_date=MONDAY, moved_to=THURSDAY)
        self.assertEqual(override.effective_date, THURSDAY)

    def test_deleting_the_series_removes_its_exceptions(self):
        EventOccurrence.objects.create(event=self.data.standup,
                                       original_date=MONDAY, is_cancelled=True)
        self.data.standup.delete()
        self.assertEqual(EventOccurrence.objects.count(), 0)


class ReminderLogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_the_same_reminder_cannot_be_logged_twice(self):
        """This constraint *is* the idempotency feature. Without it, "run every
        ten minutes" would mean exactly that."""
        ReminderLog.objects.create(source_key="task:1", event_date=MONDAY,
                                   user=self.data.anna)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            ReminderLog.objects.create(source_key="task:1", event_date=MONDAY,
                                       user=self.data.anna)

    def test_the_same_series_on_a_different_date_is_a_different_reminder(self):
        """Otherwise reminding about one stand-up silences every future one."""
        ReminderLog.objects.create(source_key="event:1@2026-07-06",
                                   event_date=MONDAY, user=self.data.anna)
        ReminderLog.objects.create(source_key="event:1@2026-07-13",
                                   event_date=MONDAY + timedelta(days=7),
                                   user=self.data.anna)
        self.assertEqual(ReminderLog.objects.count(), 2)

    def test_two_people_are_reminded_about_the_same_thing_separately(self):
        ReminderLog.objects.create(source_key="task:1", event_date=MONDAY,
                                   user=self.data.anna)
        ReminderLog.objects.create(source_key="task:1", event_date=MONDAY,
                                   user=self.data.bilal)
        self.assertEqual(ReminderLog.objects.count(), 2)

    def test_a_reminder_and_a_deadline_alert_are_separate_deliveries(self):
        ReminderLog.objects.create(source_key="task:1", event_date=MONDAY,
                                   user=self.data.anna,
                                   kind=ReminderLog.Kind.REMINDER)
        ReminderLog.objects.create(source_key="task:1", event_date=MONDAY,
                                   user=self.data.anna,
                                   kind=ReminderLog.Kind.DEADLINE)
        self.assertEqual(ReminderLog.objects.count(), 2)

    def test_purging_drops_old_rows_and_keeps_recent_ones(self):
        """An idempotency guard that never forgets is an ever-growing table."""
        ReminderLog.objects.create(source_key="old", event_date=date(2020, 1, 1),
                                   user=self.data.anna)
        ReminderLog.objects.create(source_key="new", event_date=date(2099, 1, 1),
                                   user=self.data.anna)
        ReminderLog.purge_before()
        self.assertEqual(
            list(ReminderLog.objects.values_list("source_key", flat=True)),
            ["new"])

    def test_removing_a_person_removes_their_ledger(self):
        person = make_user("temp")
        ReminderLog.objects.create(source_key="task:9", event_date=MONDAY,
                                   user=person)
        person.delete()
        self.assertEqual(ReminderLog.objects.count(), 0)
