"""Reminders and deadline alerts.

The behaviour worth proving here is not that a notification appears — it is that
a *second* one does not. `ReminderLog` is what makes the command safe to run
every ten minutes, and everything below exists to check that guard actually
holds under repeat runs, races and a scheduler that was down for a week.
"""
from datetime import datetime, time, timedelta

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from calendar_hub import reminders
from calendar_hub.models import CalendarEvent, EventOccurrence, ReminderLog
from core.models import Notification
from projects.models import Task

from .factories import MONDAY, TUESDAY, Scenario


def at(day, hour, minute=0):
    """A localised datetime, so the arithmetic matches what the code does."""
    naive = datetime.combine(day, time(hour, minute))
    return timezone.make_aware(naive) if timezone.is_aware(
        timezone.localtime()) else naive


class MeetingReminderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_meeting_reminds_its_attendees_at_the_right_moment(self):
        # The client review is 14:00 Tuesday with 60 minutes of notice.
        due = reminders.meeting_reminders(now=at(TUESDAY, 13, 0))
        recipients = {user.username for _, _, user, _, _ in due}
        self.assertEqual(recipients, {"anna", "pm"})

    def test_nothing_fires_before_the_reminder_moment(self):
        self.assertEqual(reminders.meeting_reminders(now=at(TUESDAY, 11, 0)), [])

    def test_nothing_fires_once_the_meeting_has_started(self):
        """Reminding someone about a meeting they are already in is worse than
        saying nothing."""
        self.assertEqual(reminders.meeting_reminders(now=at(TUESDAY, 14, 30)), [])

    def test_a_long_outage_does_not_produce_a_backlog_blast(self):
        """If the scheduler was down for a week, the run that comes back up
        must not fire hundreds of reminders for meetings that already happened."""
        late = at(TUESDAY, 13, 0) + reminders.MAX_LATENESS + timedelta(minutes=5)
        self.assertEqual(reminders.meeting_reminders(now=late), [])

    def test_the_organiser_is_reminded_even_without_being_an_attendee(self):
        event = CalendarEvent.objects.create(
            title="Solo prep", start_date=TUESDAY, start_time=time(9, 0),
            reminder_minutes=30, created_by=self.data.bilal)
        due = reminders.meeting_reminders(now=at(TUESDAY, 8, 30))
        self.assertIn(self.data.bilal.username,
                      {user.username for key, _, user, _, _ in due
                       if key == f"event:{event.pk}"})

    def test_an_event_with_no_reminder_set_never_fires(self):
        self.data.meeting.reminder_minutes = None
        self.data.meeting.save()
        due = reminders.meeting_reminders(now=at(TUESDAY, 13, 0))
        self.assertFalse(any(key == f"event:{self.data.meeting.pk}"
                             for key, _, _, _, _ in due))

    def test_a_recurring_series_reminds_every_week(self):
        """The stand-up is 09:30 Monday with 15 minutes of notice."""
        first = reminders.meeting_reminders(now=at(MONDAY, 9, 15))
        later = reminders.meeting_reminders(
            now=at(MONDAY + timedelta(days=7), 9, 15))
        self.assertTrue(first)
        self.assertTrue(later)
        # Different keys, so reminding about one never silences the other.
        self.assertNotEqual({k for k, _, _, _, _ in first},
                            {k for k, _, _, _, _ in later})

    def test_a_cancelled_occurrence_sends_no_reminder(self):
        EventOccurrence.objects.create(
            event=self.data.standup, original_date=MONDAY, is_cancelled=True)
        due = reminders.meeting_reminders(now=at(MONDAY, 9, 15))
        self.assertFalse(any(key.startswith(f"event:{self.data.standup.pk}@")
                             for key, _, _, _, _ in due))

    def test_a_moved_occurrence_reminds_on_its_new_day(self):
        EventOccurrence.objects.create(
            event=self.data.standup, original_date=MONDAY, moved_to=TUESDAY)
        self.assertFalse(any(key.startswith(f"event:{self.data.standup.pk}@")
                             for key, _, _, _, _ in
                             reminders.meeting_reminders(now=at(MONDAY, 9, 15))))
        self.assertTrue(any(key.startswith(f"event:{self.data.standup.pk}@")
                            for key, _, _, _, _ in
                            reminders.meeting_reminders(now=at(TUESDAY, 9, 15))))


class DeadlineAlertTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_task_due_tomorrow_alerts_its_assignee(self):
        due = reminders.deadline_alerts(today=MONDAY - timedelta(days=1))
        self.assertEqual([(user.username, text.endswith("is due tomorrow"))
                          for _, _, user, text, _ in due],
                         [("anna", True)])

    def test_nothing_alerts_two_days_out(self):
        """"Due in a fortnight" is noise that trains people to ignore the bell."""
        self.assertEqual(
            reminders.deadline_alerts(today=MONDAY - timedelta(days=2)), [])

    def test_an_unassigned_task_alerts_nobody(self):
        Task.objects.create(project=self.data.project, title="Orphan",
                            due_date=MONDAY)
        due = reminders.deadline_alerts(today=MONDAY - timedelta(days=1))
        self.assertNotIn("Orphan", " ".join(text for _, _, _, text, _ in due))

    def test_sources_that_name_nobody_opt_out_rather_than_guess(self):
        """Milestones and project deadlines have no owner field, and document
        review visibility depends on who is asking — a background job has no
        viewer to check that against. See the limitation in reminders.py."""
        from calendar_hub import sources

        opted_in = {s.key for s in sources.all_sources()
                    if s.sends_deadline_alerts}
        self.assertEqual(opted_in, {"task"})


class DeliveryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_running_twice_notifies_once(self):
        """This is the whole reason `ReminderLog` exists."""
        now = at(TUESDAY, 13, 0)
        first = reminders.run(now=now)
        after_first = Notification.objects.count()
        second = reminders.run(now=now)

        self.assertGreater(first["sent"], 0)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(Notification.objects.count(), after_first)

    def test_a_notification_and_its_ledger_row_land_together(self):
        """A notification without its ledger row is re-sent forever; a ledger
        row without its notification silences one that never arrived."""
        reminders.run(now=at(TUESDAY, 13, 0))
        self.assertEqual(ReminderLog.objects.count(),
                         Notification.objects.count())

    def test_a_dry_run_sends_and_logs_nothing(self):
        result = reminders.run(now=at(TUESDAY, 13, 0), dry_run=True)
        self.assertGreater(result["reminders"], 0)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(ReminderLog.objects.count(), 0)
        self.assertEqual(Notification.objects.count(), 0)

    def test_an_already_logged_delivery_is_skipped(self):
        ReminderLog.objects.create(
            source_key=f"event:{self.data.meeting.pk}", event_date=TUESDAY,
            user=self.data.anna, kind=ReminderLog.Kind.REMINDER)
        reminders.run(now=at(TUESDAY, 13, 0))
        self.assertEqual(
            Notification.objects.filter(user=self.data.anna).count(), 0)

    def test_an_inactive_person_is_not_reminded(self):
        self.data.anna.is_active = False
        self.data.anna.save()
        reminders.run(now=at(TUESDAY, 13, 0))
        self.assertEqual(
            Notification.objects.filter(user=self.data.anna).count(), 0)


class CommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_the_command_runs_and_reports(self):
        from io import StringIO

        out = StringIO()
        call_command("send_calendar_reminders", stdout=out)
        self.assertIn("notification", out.getvalue())

    def test_the_dry_run_flag_reaches_the_engine(self):
        from io import StringIO

        call_command("send_calendar_reminders", "--dry-run", stdout=StringIO())
        self.assertEqual(ReminderLog.objects.count(), 0)

    def test_the_purge_flag_drops_old_ledger_rows(self):
        from datetime import date
        from io import StringIO

        ReminderLog.objects.create(source_key="ancient",
                                   event_date=date(2019, 1, 1),
                                   user=self.data.anna)
        call_command("send_calendar_reminders", "--purge", stdout=StringIO())
        self.assertFalse(ReminderLog.objects.filter(source_key="ancient").exists())
