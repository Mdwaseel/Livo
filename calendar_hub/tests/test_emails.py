"""Meeting invitations, changes and cancellations.

Two things are worth proving here, and the second is the one that decides
whether people keep the feature switched on.

**The mail carries a real appointment.** An invitation whose `.ics` is missing,
malformed, or missing its recurrence rule is an email that makes somebody type
the meeting into their own calendar by hand — at which point they may as well
not have got it. So the attachment is asserted on directly: METHOD, UID, RRULE,
attendees, alarm.

**The mail is rare.** A calendar that mails eight people every time a typo is
fixed in a description gets filtered to junk within a fortnight, and then the
cancellation nobody sees is the one that leaves somebody sitting in a room
alone. Most of the tests below are therefore about who does *not* get an email.
"""
from datetime import time, timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from calendar_hub import emails
from calendar_hub.models import CalendarEvent

from .factories import MONDAY, THURSDAY, TUESDAY, Scenario

SETTINGS = dict(
    CALENDAR_INVITE_EMAILS=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    SITE_URL="https://os.example.com",
)


def calendars(message):
    """The text/calendar attachments on a sent message, unfolded.

    RFC 5545 breaks any line over 75 octets and continues it after a CRLF and a
    space, so a long ATTENDEE arrives as `…:mailt\\r\\n o:anna@…`. Folding is
    correct and is tested on its own in `test_ical.py`; undoing it here keeps
    these assertions about *what the file says* rather than about where the
    line breaks landed.
    """
    return [content.replace("\r\n ", "")
            for _, content, mimetype in message.attachments
            if mimetype == "text/calendar"]


@override_settings(**SETTINGS)
class InviteContentTests(TestCase):
    """What actually arrives."""

    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        mail.outbox = []

    def test_an_invitation_reaches_each_attendee_separately(self):
        """One message each, not one message with everybody in `To` — a private
        one-to-one must not publish its guest list in a header."""
        sent = emails.send_event_invite(
            self.data.meeting, [self.data.anna, self.data.bilal],
            change="invited", actor=self.data.pm)

        self.assertEqual(sent, 2)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual([message.to for message in mail.outbox],
                         [[self.data.anna.email], [self.data.bilal.email]])

    def test_the_body_carries_the_details_somebody_needs_to_attend(self):
        emails.send_event_invite(self.data.meeting, [self.data.anna],
                                 change="invited", actor=self.data.pm)
        body = mail.outbox[0].body

        self.assertIn("Client review", body)          # what
        self.assertIn("14:00", body)                  # when
        self.assertIn("Tuesday 07 July 2026", body)
        self.assertIn("Meet", body)                   # where
        self.assertIn("Website", body)                # which project
        self.assertIn("Acme", body)                   # whose
        self.assertIn("Pm", body)                     # who called it
        self.assertIn("60 minutes before", body)      # the reminder they'll get

    def test_the_link_survives_having_no_request(self):
        """A drag-and-drop reschedule has no request to take a host from, so the
        link comes from SITE_URL or the email goes out without one."""
        emails.send_event_invite(self.data.meeting, [self.data.anna],
                                 change="invited", actor=self.data.pm)
        self.assertIn("https://os.example.com/calendar/events/", mail.outbox[0].body)

    def test_a_html_alternative_is_attached(self):
        emails.send_event_invite(self.data.meeting, [self.data.anna],
                                 change="invited", actor=self.data.pm)
        alternatives = mail.outbox[0].alternatives
        self.assertEqual(len(alternatives), 1)
        self.assertEqual(alternatives[0][1], "text/html")
        self.assertIn("Client review", alternatives[0][0])

    def test_an_all_day_event_says_all_day_rather_than_a_time(self):
        event = CalendarEvent.objects.create(
            title="Offsite", start_date=THURSDAY, created_by=self.data.pm)
        event.attendees.set([self.data.anna])
        emails.send_event_invite(event, [self.data.anna], actor=self.data.pm)
        self.assertIn("all day", mail.outbox[0].body)

    def test_a_multi_day_event_names_both_ends(self):
        event = CalendarEvent.objects.create(
            title="Offsite", start_date=THURSDAY,
            end_date=THURSDAY + timedelta(days=2), created_by=self.data.pm)
        label = emails.when_label(event)
        self.assertIn("Thursday 09 July 2026", label)
        self.assertIn("Saturday 11 July 2026", label)

    def test_the_subject_says_which_of_the_three_it_is(self):
        for change, expected in (("invited", "Client review"),
                                 ("updated", "Updated: Client review"),
                                 ("cancelled", "Cancelled: Client review")):
            with self.subTest(change=change):
                mail.outbox = []
                emails.send_event_invite(self.data.meeting, [self.data.anna],
                                         change=change, actor=self.data.pm)
                self.assertTrue(mail.outbox[0].subject.startswith(expected),
                                mail.outbox[0].subject)


@override_settings(**SETTINGS)
class AttachmentTests(TestCase):
    """The `.ics` is the part that does the work. It has to be right."""

    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        mail.outbox = []

    def test_an_invitation_attaches_a_request(self):
        """METHOD:REQUEST is what makes Gmail and Outlook render an
        add-to-calendar strip instead of a file to open by hand."""
        emails.send_event_invite(self.data.meeting, [self.data.anna],
                                 change="invited", actor=self.data.pm)
        [ics] = calendars(mail.outbox[0])

        self.assertIn("METHOD:REQUEST", ics)
        self.assertIn("BEGIN:VEVENT", ics)
        self.assertIn(f"UID:event-{self.data.meeting.pk}@livo", ics)
        self.assertIn("SUMMARY:Client review", ics)
        self.assertIn("DTSTART:20260707T140000", ics)
        self.assertIn("DTEND:20260707T150000", ics)
        self.assertIn("STATUS:CONFIRMED", ics)

    def test_a_cancellation_attaches_a_cancel_for_the_same_uid(self):
        """Same UID, opposite METHOD — that is what clears the appointment from
        the recipient's calendar instead of leaving a ghost on it."""
        emails.send_event_invite(self.data.meeting, [self.data.anna],
                                 change="cancelled", actor=self.data.pm)
        [ics] = calendars(mail.outbox[0])

        self.assertIn("METHOD:CANCEL", ics)
        self.assertIn(f"UID:event-{self.data.meeting.pk}@livo", ics)
        self.assertIn("STATUS:CANCELLED", ics)

    def test_a_repeating_event_travels_as_one_repeating_appointment(self):
        """Without the RRULE the recipient gets a single Monday stand-up and
        never hears about the other fifty."""
        emails.send_event_invite(self.data.standup, [self.data.anna],
                                 actor=self.data.manager)
        [ics] = calendars(mail.outbox[0])

        self.assertIn("RRULE:FREQ=WEEKLY;BYDAY=MO", ics)

    def test_an_interval_and_an_end_reach_the_rule(self):
        self.data.standup.interval = 2
        self.data.standup.repeat_until = MONDAY + timedelta(days=70)
        self.data.standup.save()

        emails.send_event_invite(self.data.standup, [self.data.anna],
                                 actor=self.data.manager)
        [ics] = calendars(mail.outbox[0])
        self.assertIn("FREQ=WEEKLY", ics)
        self.assertIn("INTERVAL=2", ics)
        self.assertIn("UNTIL=20260914T235959", ics)

    def test_a_count_is_used_when_there_is_no_end_date(self):
        self.data.standup.repeat_count = 6
        self.data.standup.save()
        emails.send_event_invite(self.data.standup, [self.data.anna],
                                 actor=self.data.manager)
        self.assertIn("COUNT=6", calendars(mail.outbox[0])[0])

    def test_an_all_day_event_is_encoded_as_a_date_with_an_exclusive_end(self):
        """A DTEND equal to DTSTART is a zero-length event, which some clients
        silently drop."""
        event = CalendarEvent.objects.create(
            title="Offsite", start_date=THURSDAY,
            end_date=THURSDAY + timedelta(days=1), created_by=self.data.pm)
        event.attendees.set([self.data.anna])
        emails.send_event_invite(event, [self.data.anna], actor=self.data.pm)
        [ics] = calendars(mail.outbox[0])

        self.assertIn("DTSTART;VALUE=DATE:20260709", ics)
        self.assertIn("DTEND;VALUE=DATE:20260711", ics)

    def test_the_organiser_and_attendees_are_named(self):
        emails.send_event_invite(self.data.meeting, [self.data.anna],
                                 actor=self.data.manager)
        [ics] = calendars(mail.outbox[0])

        self.assertIn(f"ORGANIZER;CN=Pm:mailto:{self.data.pm.email}", ics)
        self.assertIn(f"mailto:{self.data.anna.email}", ics)
        self.assertIn("RSVP=TRUE", ics)

    def test_the_reminder_becomes_an_alarm(self):
        emails.send_event_invite(self.data.meeting, [self.data.anna],
                                 actor=self.data.pm)
        [ics] = calendars(mail.outbox[0])

        self.assertIn("BEGIN:VALARM", ics)
        self.assertIn("TRIGGER:-PT60M", ics)

    def test_no_reminder_means_no_alarm(self):
        self.data.meeting.reminder_minutes = None
        self.data.meeting.save()
        emails.send_event_invite(self.data.meeting, [self.data.anna],
                                 actor=self.data.pm)
        self.assertNotIn("BEGIN:VALARM", calendars(mail.outbox[0])[0])

    def test_a_title_with_a_comma_is_escaped(self):
        """An unescaped comma splits the property value and corrupts the file."""
        self.data.meeting.title = "Review: Acme, Inc."
        self.data.meeting.save()
        emails.send_event_invite(self.data.meeting, [self.data.anna],
                                 actor=self.data.pm)
        self.assertIn("SUMMARY:Review: Acme\\, Inc.", calendars(mail.outbox[0])[0])


@override_settings(**SETTINGS)
class WhoIsNotEmailedTests(TestCase):
    """The negative rules. These are what keep the feature out of junk."""

    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        mail.outbox = []

    def test_nobody_is_emailed_about_their_own_decision(self):
        emails.send_event_invite(self.data.meeting,
                                 [self.data.anna, self.data.pm],
                                 actor=self.data.pm)
        self.assertEqual([message.to for message in mail.outbox],
                         [[self.data.anna.email]])

    def test_a_deactivated_person_is_skipped(self):
        self.data.anna.is_active = False
        self.data.anna.save()
        self.assertEqual(
            emails.send_event_invite(self.data.meeting, [self.data.anna],
                                     actor=self.data.pm), 0)
        self.assertEqual(mail.outbox, [])

    def test_somebody_with_no_address_is_skipped(self):
        self.data.anna.email = ""
        self.data.anna.save()
        self.assertEqual(
            emails.send_event_invite(self.data.meeting, [self.data.anna],
                                     actor=self.data.pm), 0)

    @override_settings(CALENDAR_INVITE_EMAILS=False)
    def test_the_setting_turns_the_whole_channel_off(self):
        self.assertEqual(
            emails.send_event_invite(self.data.meeting, [self.data.anna],
                                     actor=self.data.pm), 0)
        self.assertEqual(mail.outbox, [])

    def test_a_broken_mail_server_does_not_break_the_meeting(self):
        """The event is already saved and visible by the time this runs, so a
        failure here is the operator's problem, not the organiser's."""
        with override_settings(
                EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
                EMAIL_HOST="", EMAIL_PORT=1):
            with self.assertLogs("core.mailer", level="ERROR"):
                sent = emails.send_event_invite(
                    self.data.meeting, [self.data.anna], actor=self.data.pm)
        self.assertEqual(sent, 0)


class MaterialChangeTests(TestCase):
    """Which edits are worth interrupting eight people for."""

    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_moving_the_meeting_is_material(self):
        before = emails.snapshot(self.data.meeting)
        self.data.meeting.start_date = THURSDAY
        self.assertTrue(emails.changed_materially(before, self.data.meeting))

    def test_changing_the_time_is_material(self):
        before = emails.snapshot(self.data.meeting)
        self.data.meeting.start_time = time(9, 0)
        self.assertTrue(emails.changed_materially(before, self.data.meeting))

    def test_changing_where_it_happens_is_material(self):
        for field, value in (("location", "Boardroom"),
                             ("meeting_url", "https://meet.example.com/x")):
            with self.subTest(field=field):
                before = emails.snapshot(self.data.meeting)
                setattr(self.data.meeting, field, value)
                self.assertTrue(
                    emails.changed_materially(before, self.data.meeting))

    def test_a_reworded_description_is_not(self):
        """Nobody rearranges their day because a note got clearer."""
        before = emails.snapshot(self.data.meeting)
        self.data.meeting.description = "Bring the deck."
        self.assertFalse(emails.changed_materially(before, self.data.meeting))

    def test_changing_the_reminder_or_the_visibility_is_not(self):
        for field, value in (("reminder_minutes", 15),
                             ("visibility", CalendarEvent.Visibility.PRIVATE)):
            with self.subTest(field=field):
                before = emails.snapshot(self.data.meeting)
                setattr(self.data.meeting, field, value)
                self.assertFalse(
                    emails.changed_materially(before, self.data.meeting))

    def test_a_new_event_has_nothing_to_compare_against(self):
        self.assertFalse(emails.changed_materially(None, self.data.meeting))


@override_settings(**SETTINGS)
class ThroughTheViewsTests(TestCase):
    """The wiring: does using the app actually send these?"""

    def setUp(self):
        self.data = Scenario()
        self.client.force_login(self.data.pm)
        mail.outbox = []

    def _post(self, url, **overrides):
        form = {
            "kind": CalendarEvent.Kind.MEETING,
            "title": "Sprint kickoff",
            "start_date": TUESDAY.isoformat(),
            "start_time": "10:00",
            "end_time": "11:00",
            "frequency": CalendarEvent.Frequency.NONE,
            "interval": "1",
            "visibility": CalendarEvent.Visibility.AGENCY,
            "reminder_minutes": "30",
            "attendees": [str(self.data.anna.pk)],
        }
        form.update(overrides)
        return self.client.post(url, form)

    def test_scheduling_a_meeting_invites_the_attendees(self):
        self._post(reverse("calendar_hub:event_create"))

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.data.anna.email])
        self.assertIn("Sprint kickoff", message.subject)
        self.assertIn("METHOD:REQUEST", calendars(message)[0])

    def test_the_organiser_does_not_invite_themselves(self):
        self._post(reverse("calendar_hub:event_create"),
                   attendees=[str(self.data.pm.pk)])
        self.assertEqual(mail.outbox, [])

    def test_moving_a_meeting_tells_the_people_already_on_it(self):
        url = reverse("calendar_hub:event_edit", args=[self.data.meeting.pk])
        self._post(url, title="Client review", start_date=THURSDAY.isoformat(),
                   attendees=[str(self.data.anna.pk)])

        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(mail.outbox[0].subject.startswith("Updated:"))
        self.assertEqual(mail.outbox[0].to, [self.data.anna.email])

    def _unchanged(self, **overrides):
        """The edit form as it arrives when nothing material was touched.

        Every material field has to be echoed back — the editor is a plain POST,
        so a field left out of the form is a field cleared, and clearing the
        location genuinely *is* a change worth telling people about.
        """
        form = {
            "title": "Client review",
            "start_date": self.data.meeting.start_date.isoformat(),
            "start_time": "14:00",
            "end_time": "15:00",
            "location": self.data.meeting.location,
            "attendees": [str(self.data.anna.pk)],
        }
        form.update(overrides)
        return form

    def test_a_cosmetic_edit_tells_nobody(self):
        url = reverse("calendar_hub:event_edit", args=[self.data.meeting.pk])
        self._post(url, **self._unchanged(description="Bring the deck."))
        self.assertEqual(mail.outbox, [])

    def test_clearing_the_location_does_tell_people(self):
        """The other half of the rule above: turning up at the old place is
        exactly the failure this channel exists to prevent."""
        url = reverse("calendar_hub:event_edit", args=[self.data.meeting.pk])
        self._post(url, **self._unchanged(location=""))
        self.assertEqual([message.to for message in mail.outbox],
                         [[self.data.anna.email]])

    def test_adding_one_person_invites_only_them(self):
        """Adding Bilal must not re-invite Anna, who has not been affected."""
        url = reverse("calendar_hub:event_edit", args=[self.data.meeting.pk])
        self._post(url, **self._unchanged(
            attendees=[str(self.data.anna.pk), str(self.data.bilal.pk)]))

        self.assertEqual([message.to for message in mail.outbox],
                         [[self.data.bilal.email]])
        self.assertIn("Client review", mail.outbox[0].subject)
        self.assertFalse(mail.outbox[0].subject.startswith("Updated:"))

    def test_removing_a_meeting_cancels_it_for_everyone_on_it(self):
        self.client.force_login(self.data.manager)
        mail.outbox = []
        self.client.post(
            reverse("calendar_hub:event_delete", args=[self.data.meeting.pk]))

        # Anna was an attendee; the PM organised it. Both hear, the manager who
        # pressed the button does not.
        self.assertEqual(sorted(address for message in mail.outbox
                                for address in message.to),
                         sorted([self.data.anna.email, self.data.pm.email]))
        self.assertTrue(all(message.subject.startswith("Cancelled:")
                            for message in mail.outbox))
        self.assertIn("METHOD:CANCEL", calendars(mail.outbox[0])[0])
