"""The screens, the access rules, and the drag-and-drop endpoint.

The permission tests are the important ones here. The calendar reads six other
apps and can write back to four of them, so the thing most worth proving is
that it never becomes a way around anybody else's rules.
"""
import json
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse

from calendar_hub.models import CalendarEvent, EventOccurrence
from core.models import Notification
from projects.models import Milestone

from .factories import (FRIDAY, MONDAY, THURSDAY, WEDNESDAY, Scenario,
                        a_private_event, forget_perms, set_perm)

PAGES = ("month", "week", "day", "agenda")


class AccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(reverse("calendar_hub:month"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_every_working_role_can_open_the_calendar(self):
        """`calendar: RW` is in the RBAC seed's `_BASE`, granted to every role
        that does work here. The key was reserved with no model behind it; this
        module fills it, and nobody's matrix had to be rewritten."""
        for person in (self.data.manager, self.data.pm, self.data.anna,
                       self.data.bilal, self.data.sales):
            with self.subTest(user=person.username):
                self.client.force_login(person)
                self.assertEqual(
                    self.client.get(reverse("calendar_hub:month")).status_code,
                    200)

    def test_losing_calendar_view_closes_every_page(self):
        set_perm("Developer", "calendar", "view", False)
        self.client.force_login(self.data.anna)
        for name in PAGES:
            with self.subTest(page=name):
                response = self.client.get(reverse(f"calendar_hub:{name}"))
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("core:dashboard"), response["Location"])

    def test_granting_calendar_view_needs_no_deploy(self):
        """Access is a checkbox in the matrix, not a role name in code."""
        set_perm("Sales", "calendar", "view", False)
        self.client.force_login(self.data.sales)
        self.assertEqual(
            self.client.get(reverse("calendar_hub:month")).status_code, 302)
        set_perm("Sales", "calendar", "view", True)
        self.assertEqual(
            self.client.get(reverse("calendar_hub:month")).status_code, 200)

    def test_creating_needs_calendar_create(self):
        set_perm("Developer", "calendar", "create", False)
        self.client.force_login(self.data.anna)
        response = self.client.get(reverse("calendar_hub:event_create"))
        self.assertEqual(response.status_code, 302)


class PageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        self.client.force_login(self.data.manager)

    def test_every_page_renders(self):
        for name in PAGES:
            with self.subTest(page=name):
                response = self.client.get(
                    reverse(f"calendar_hub:{name}") + f"?date={MONDAY}")
                self.assertEqual(response.status_code, 200)

    def test_the_month_grid_reaches_the_template(self):
        response = self.client.get(reverse("calendar_hub:month") + "?date=2026-07-01")
        self.assertEqual(len(response.context["weeks"]), 5)

    def test_a_task_is_visible_on_the_month(self):
        response = self.client.get(reverse("calendar_hub:month") + "?date=2026-07-01")
        self.assertContains(response, "Build header")

    def test_the_legend_labels_every_kind_in_text(self):
        """Colour is never the only signal — the name has to be readable."""
        response = self.client.get(reverse("calendar_hub:month") + "?date=2026-07-01")
        for label in ("Tasks", "Milestones", "Meetings", "Birthdays"):
            self.assertContains(response, label)

    def test_a_filter_survives_switching_view(self):
        """One URL grammar across every screen — the resource planner's."""
        url = (reverse("calendar_hub:week")
               + f"?date={MONDAY}&employee={self.data.anna.pk}")
        response = self.client.get(url)
        self.assertEqual(response.context["query"].employee_id, self.data.anna.pk)
        self.assertIn(f"employee={self.data.anna.pk}",
                      response.context["querystring"])

    def test_an_unknown_category_is_dropped_rather_than_matched(self):
        """An unrecognised ?kind= would otherwise match nothing and read as an
        empty calendar."""
        response = self.client.get(
            reverse("calendar_hub:month") + "?kind=nonsense")
        self.assertEqual(response.context["query"].kinds, frozenset())
        self.assertGreater(response.context["total"], 0)

    def test_search_narrows_the_results(self):
        everything = self.client.get(
            reverse("calendar_hub:month") + "?date=2026-07-01")
        narrowed = self.client.get(
            reverse("calendar_hub:month") + "?date=2026-07-01&q=header")
        self.assertLess(narrowed.context["total"], everything.context["total"])
        self.assertContains(narrowed, "Build header")

    def test_a_malformed_date_falls_back_to_today_instead_of_erroring(self):
        response = self.client.get(reverse("calendar_hub:month") + "?date=nonsense")
        self.assertEqual(response.status_code, 200)

    def test_today_redirects_to_the_day_view(self):
        response = self.client.get(reverse("calendar_hub:today"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("calendar_hub:day"), response["Location"])

    def test_the_day_panel_returns_a_fragment(self):
        response = self.client.get(
            reverse("calendar_hub:day_panel", args=[str(MONDAY)]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<html")
        self.assertContains(response, "Build header")

    def test_the_day_panel_rejects_a_date_the_router_cannot_parse(self):
        response = self.client.get("/calendar/day/not-a-date/panel/")
        self.assertEqual(response.status_code, 404)


class ExportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        self.client.force_login(self.data.manager)

    def test_the_ics_export_is_a_calendar_file(self):
        response = self.client.get(
            reverse("calendar_hub:ics", args=["month"]) + "?date=2026-07-01")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/calendar", response["Content-Type"])
        self.assertIn("attachment", response["Content-Disposition"])
        body = response.content.decode("utf-8")
        self.assertTrue(body.startswith("BEGIN:VCALENDAR"))
        self.assertIn("Build header", body)

    def test_an_unknown_period_is_a_404(self):
        response = self.client.get(reverse("calendar_hub:ics", args=["decade"]))
        self.assertEqual(response.status_code, 404)

    def test_the_export_honours_the_filters_on_screen(self):
        """A download that doesn't match the screen is a support ticket waiting
        to happen."""
        response = self.client.get(
            reverse("calendar_hub:ics", args=["month"])
            + "?date=2026-07-01&kind=birthday")
        body = response.content.decode("utf-8")
        self.assertIn("birthday", body.lower())
        self.assertNotIn("Build header", body)


class MoveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def move(self, **payload):
        return self.client.post(reverse("calendar_hub:move"),
                                data=json.dumps(payload),
                                content_type="application/json")

    def test_a_manager_can_drag_a_task_to_another_day(self):
        self.client.force_login(self.data.manager)
        response = self.move(source="task", id=self.data.task.pk,
                             date=str(THURSDAY))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.data.task.refresh_from_db()
        self.assertEqual(self.data.task.due_date, THURSDAY)

    def test_moving_a_task_tells_the_person_who_has_to_do_it(self):
        self.client.force_login(self.data.manager)
        self.move(source="task", id=self.data.task.pk, date=str(THURSDAY))
        self.assertTrue(Notification.objects.filter(user=self.data.anna).exists())

    def test_dragging_a_task_needs_the_task_modules_permission(self):
        """Not the calendar's. The calendar must not become a way to edit half
        the system with one permission."""
        set_perm("Developer", "tasks", "edit", False)
        set_perm("Developer", "calendar", "edit", True)
        forget_perms(self.data.anna)
        self.client.force_login(self.data.anna)
        response = self.move(source="task", id=self.data.task.pk,
                             date=str(THURSDAY))
        self.assertEqual(response.status_code, 403)
        self.data.task.refresh_from_db()
        self.assertEqual(self.data.task.due_date, MONDAY)

    def test_a_milestone_moves_without_changing_its_status(self):
        self.client.force_login(self.data.manager)
        self.move(source="milestone", id=self.data.milestone.pk,
                  date=str(FRIDAY))
        self.data.milestone.refresh_from_db()
        self.assertEqual(self.data.milestone.due_date, FRIDAY)
        self.assertEqual(self.data.milestone.status, Milestone.Status.PENDING)
        self.assertIsNone(self.data.milestone.completed_on)

    def test_a_project_deadline_cannot_be_dragged_before_its_start(self):
        """A negative schedule is a typo, not a plan."""
        self.client.force_login(self.data.manager)
        response = self.move(source="project", id=self.data.project.pk,
                             date=str(self.data.project.start_date
                                      - timedelta(days=5)))
        self.assertEqual(response.status_code, 400)
        self.assertIn("starts on", response.json()["error"])

    def test_a_document_review_deadline_moves(self):
        self.client.force_login(self.data.manager)
        self.move(source="document", id=self.data.document.pk, date=str(FRIDAY))
        self.data.document.refresh_from_db()
        self.assertEqual(self.data.document.review_due_date, FRIDAY)

    def test_a_financial_document_cannot_be_moved_by_someone_who_cannot_see_it(self):
        """`can_move` only proves the viewer may edit documents in general — it
        says nothing about whether they may see *this* one.

        A Developer with `documents.edit` still holds neither `finance.view` nor
        `invoices.view`, so the invoice has to come back as missing rather than
        as forbidden: a 403 would confirm it exists.
        """
        set_perm("Developer", "documents", "edit", True)
        forget_perms(self.data.anna)
        self.client.force_login(self.data.anna)
        # The non-financial document in the same project moves fine, which is
        # what makes the refusal below about the *type* and not about the role.
        self.assertTrue(self.move(source="document", id=self.data.document.pk,
                                  date=str(FRIDAY)).json()["ok"])
        response = self.move(source="document", id=self.data.invoice.pk,
                             date=str(FRIDAY))
        self.assertEqual(response.status_code, 404)
        self.data.invoice.refresh_from_db()
        self.assertEqual(self.data.invoice.review_due_date, THURSDAY)

    def test_a_one_off_meeting_moves_wholesale(self):
        self.client.force_login(self.data.manager)
        self.move(source="event", id=self.data.meeting.pk, date=str(FRIDAY))
        self.data.meeting.refresh_from_db()
        self.assertEqual(self.data.meeting.start_date, FRIDAY)

    def test_a_multi_day_event_keeps_its_length_when_moved(self):
        event = CalendarEvent.objects.create(
            title="Offsite", start_date=MONDAY,
            end_date=MONDAY + timedelta(days=2), created_by=self.data.manager)
        self.client.force_login(self.data.manager)
        self.move(source="event", id=event.pk, date=str(THURSDAY))
        event.refresh_from_db()
        self.assertEqual(event.start_date, THURSDAY)
        self.assertEqual(event.end_date, THURSDAY + timedelta(days=2))

    def test_dragging_one_stand_up_does_not_move_the_whole_series(self):
        """Relocating every future stand-up because somebody dragged one of
        them is both surprising and hard to undo."""
        self.client.force_login(self.data.manager)
        response = self.move(source="event", id=self.data.standup.pk,
                             date=str(WEDNESDAY), occurrence=str(MONDAY))
        self.assertTrue(response.json()["ok"])
        self.data.standup.refresh_from_db()
        self.assertEqual(self.data.standup.start_date, MONDAY)
        override = EventOccurrence.objects.get(event=self.data.standup)
        self.assertEqual(override.original_date, MONDAY)
        self.assertEqual(override.moved_to, WEDNESDAY)
        self.assertIn("rest of the series is unchanged",
                      response.json()["message"])

    def test_moving_a_recurring_event_without_naming_an_occurrence_is_refused(self):
        self.client.force_login(self.data.manager)
        response = self.move(source="event", id=self.data.standup.pk,
                             date=str(WEDNESDAY))
        self.assertEqual(response.status_code, 400)

    def test_dragging_an_occurrence_back_to_its_own_date_clears_the_exception(self):
        EventOccurrence.objects.create(event=self.data.standup,
                                       original_date=MONDAY, moved_to=WEDNESDAY)
        self.client.force_login(self.data.manager)
        self.move(source="event", id=self.data.standup.pk, date=str(MONDAY),
                  occurrence=str(MONDAY))
        self.assertFalse(EventOccurrence.objects.filter(
            event=self.data.standup).exists())

    def test_leave_cannot_be_dragged(self):
        self.client.force_login(self.data.manager)
        response = self.move(source="leave", id=self.data.leave.pk,
                             date=str(FRIDAY))
        self.assertEqual(response.status_code, 403)

    def test_an_unknown_source_is_refused(self):
        self.client.force_login(self.data.manager)
        self.assertEqual(
            self.move(source="payroll", id=1, date=str(FRIDAY)).status_code, 400)

    def test_a_source_the_viewer_cannot_see_is_indistinguishable_from_missing(self):
        self.client.force_login(self.data.sales)
        response = self.move(source="task", id=self.data.task.pk,
                             date=str(FRIDAY))
        self.assertEqual(response.status_code, 404)

    def test_a_malformed_body_is_a_400_not_a_500(self):
        self.client.force_login(self.data.manager)
        response = self.client.post(reverse("calendar_hub:move"),
                                    data="{not json",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_a_missing_record_is_a_404(self):
        self.client.force_login(self.data.manager)
        response = self.move(source="task", id=999999, date=str(FRIDAY))
        self.assertEqual(response.status_code, 404)

    def test_an_invalid_date_is_a_400(self):
        self.client.force_login(self.data.manager)
        self.assertEqual(
            self.move(source="task", id=self.data.task.pk,
                      date="the 4th of Smarch").status_code, 400)

    def test_a_get_is_not_allowed(self):
        self.client.force_login(self.data.manager)
        self.assertEqual(
            self.client.get(reverse("calendar_hub:move")).status_code, 405)

    def test_losing_calendar_view_closes_the_drag_endpoint_too(self):
        """A drag is an action on the calendar. Somebody who can't open it has
        no business posting to it, even if they hold tasks.edit."""
        set_perm("Developer", "calendar", "view", False)
        forget_perms(self.data.anna)
        self.client.force_login(self.data.anna)
        response = self.move(source="task", id=self.data.task.pk,
                             date=str(THURSDAY))
        self.assertEqual(response.status_code, 403)
        self.data.task.refresh_from_db()
        self.assertEqual(self.data.task.due_date, MONDAY)


class EventEditorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        self.client.force_login(self.data.manager)

    def payload(self, **overrides):
        data = {"kind": CalendarEvent.Kind.MEETING, "title": "Kickoff",
                "start_date": str(THURSDAY), "start_time": "10:00",
                "end_time": "11:00", "frequency": CalendarEvent.Frequency.NONE,
                "interval": "1", "visibility": CalendarEvent.Visibility.AGENCY,
                "reminder_minutes": "30"}
        data.update(overrides)
        return data

    def test_creating_an_event(self):
        response = self.client.post(reverse("calendar_hub:event_create"),
                                    self.payload(), follow=True)
        self.assertEqual(response.status_code, 200)
        event = CalendarEvent.objects.get(title="Kickoff")
        self.assertEqual(event.start_date, THURSDAY)
        self.assertEqual(event.created_by, self.data.manager)

    def test_attendees_are_invited_once(self):
        """An edit never produces a second *invitation*.

        Moving the meeting to Room 2 does tell Anna — turning up at the old room
        is the failure this whole channel exists to prevent — but it tells her
        as a change, not as a fresh invite. The distinction is the point: a
        duplicate invitation reads as noise, a change notice reads as news. See
        `calendar_hub.emails.MATERIAL_FIELDS` for which edits qualify.
        """
        self.client.post(reverse("calendar_hub:event_create"),
                         self.payload(attendees=[self.data.anna.pk]))
        event = CalendarEvent.objects.get(title="Kickoff")
        hers = Notification.objects.filter(user=self.data.anna)
        self.assertEqual(hers.count(), 1)

        self.client.post(reverse("calendar_hub:event_edit", args=[event.pk]),
                         self.payload(attendees=[self.data.anna.pk],
                                      location="Room 2"))
        self.assertEqual(hers.filter(text__contains="invited").count(), 1)
        self.assertEqual(hers.filter(text__contains="changed").count(), 1)

    def test_a_cosmetic_edit_notifies_nobody(self):
        """The other half of the rule: only changes that would move somebody's
        day are worth a second line under their bell."""
        self.client.post(reverse("calendar_hub:event_create"),
                         self.payload(attendees=[self.data.anna.pk]))
        event = CalendarEvent.objects.get(title="Kickoff")
        self.client.post(reverse("calendar_hub:event_edit", args=[event.pk]),
                         self.payload(attendees=[self.data.anna.pk],
                                      description="Bring the deck."))
        self.assertEqual(
            Notification.objects.filter(user=self.data.anna).count(), 1)

    def test_a_newly_added_attendee_is_told(self):
        self.client.post(reverse("calendar_hub:event_create"),
                         self.payload(attendees=[self.data.anna.pk]))
        event = CalendarEvent.objects.get(title="Kickoff")
        self.client.post(reverse("calendar_hub:event_edit", args=[event.pk]),
                         self.payload(attendees=[self.data.anna.pk,
                                                 self.data.bilal.pk]))
        self.assertTrue(Notification.objects.filter(user=self.data.bilal).exists())

    def test_a_title_is_required(self):
        response = self.client.post(reverse("calendar_hub:event_create"),
                                    self.payload(title="  "))
        self.assertEqual(response.status_code, 200)  # redisplayed, not saved
        self.assertFalse(CalendarEvent.objects.filter(title="").exists())

    def test_a_start_date_is_required(self):
        response = self.client.post(reverse("calendar_hub:event_create"),
                                    self.payload(start_date=""))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CalendarEvent.objects.filter(title="Kickoff").exists())

    def test_an_invalid_range_is_redisplayed_with_the_message(self):
        response = self.client.post(
            reverse("calendar_hub:event_create"),
            self.payload(end_date=str(MONDAY)), follow=True)
        self.assertContains(response, "end date")
        self.assertFalse(CalendarEvent.objects.filter(title="Kickoff").exists())

    def test_weekdays_are_dropped_when_the_event_does_not_repeat_weekly(self):
        """Otherwise the model's own validator refuses a form the user filled in
        reasonably — they changed the frequency and left the boxes ticked."""
        self.client.post(reverse("calendar_hub:event_create"),
                         self.payload(frequency=CalendarEvent.Frequency.MONTHLY,
                                      weekdays=["1", "4"]))
        event = CalendarEvent.objects.get(title="Kickoff")
        self.assertEqual(event.weekdays, "")

    def test_a_blank_reminder_means_nobody_is_reminded(self):
        """Distinct from 0, which would mean "tell me as it starts"."""
        self.client.post(reverse("calendar_hub:event_create"),
                         self.payload(reminder_minutes=""))
        self.assertIsNone(CalendarEvent.objects.get(title="Kickoff").reminder_minutes)

    def test_a_private_event_is_a_404_for_an_outsider(self):
        """A 403 would confirm it exists."""
        event = a_private_event(self.data.pm, attendees=[self.data.anna])
        self.client.force_login(self.data.bilal)
        self.assertEqual(
            self.client.get(reverse("calendar_hub:event_detail",
                                    args=[event.pk])).status_code, 404)

    def test_an_invitee_can_open_a_private_event(self):
        event = a_private_event(self.data.pm, attendees=[self.data.anna])
        self.client.force_login(self.data.anna)
        self.assertEqual(
            self.client.get(reverse("calendar_hub:event_detail",
                                    args=[event.pk])).status_code, 200)

    def test_the_organiser_can_edit_their_own_event_without_calendar_edit(self):
        """Somebody who scheduled their own one-to-one should not need a
        permission to move it, and a role edited at lunchtime shouldn't strand
        a meeting they own."""
        event = CalendarEvent.objects.create(title="Mine", start_date=THURSDAY,
                                             created_by=self.data.anna)
        set_perm("Developer", "calendar", "edit", False)
        forget_perms(self.data.anna)
        self.client.force_login(self.data.anna)
        response = self.client.get(reverse("calendar_hub:event_edit",
                                           args=[event.pk]))
        self.assertEqual(response.status_code, 200)

    def test_somebody_elses_event_needs_calendar_edit(self):
        set_perm("Developer", "calendar", "edit", False)
        forget_perms(self.data.anna)
        self.client.force_login(self.data.anna)
        response = self.client.get(
            reverse("calendar_hub:event_edit", args=[self.data.meeting.pk]))
        self.assertEqual(response.status_code, 302)

    def test_deleting_tells_the_attendees(self):
        self.client.post(reverse("calendar_hub:event_delete",
                                 args=[self.data.meeting.pk]))
        self.assertFalse(
            CalendarEvent.objects.filter(pk=self.data.meeting.pk).exists())
        self.assertTrue(Notification.objects.filter(user=self.data.anna).exists())

    def test_a_private_event_cannot_be_deleted_by_someone_who_cannot_see_it(self):
        """Holding `calendar.delete` must not let somebody remove a private
        event they were never able to see — the deletion would confirm it
        existed."""
        event = a_private_event(self.data.pm, attendees=[self.data.anna])
        self.client.force_login(self.data.bilal)
        response = self.client.post(
            reverse("calendar_hub:event_delete", args=[event.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(CalendarEvent.objects.filter(pk=event.pk).exists())

    def test_deleting_needs_permission_or_ownership(self):
        set_perm("Developer", "calendar", "delete", False)
        forget_perms(self.data.anna)
        self.client.force_login(self.data.anna)
        self.client.post(reverse("calendar_hub:event_delete",
                                 args=[self.data.meeting.pk]))
        self.assertTrue(
            CalendarEvent.objects.filter(pk=self.data.meeting.pk).exists())


class OccurrenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        self.client.force_login(self.data.manager)

    def test_skipping_one_occurrence_leaves_the_rule_alone(self):
        self.client.post(
            reverse("calendar_hub:occurrence_cancel", args=[self.data.standup.pk]),
            {"date": str(MONDAY + timedelta(days=7))})
        override = EventOccurrence.objects.get(event=self.data.standup)
        self.assertTrue(override.is_cancelled)
        self.data.standup.refresh_from_db()
        self.assertEqual(self.data.standup.frequency,
                         CalendarEvent.Frequency.WEEKLY)

    def test_restoring_puts_the_occurrence_back_in_the_series(self):
        EventOccurrence.objects.create(event=self.data.standup,
                                       original_date=MONDAY, is_cancelled=True)
        self.client.post(reverse("calendar_hub:occurrence_restore",
                                 args=[self.data.standup.pk, str(MONDAY)]))
        self.assertFalse(EventOccurrence.objects.filter(
            event=self.data.standup).exists())

    def test_skipping_without_a_date_is_a_harmless_no_op(self):
        self.client.post(
            reverse("calendar_hub:occurrence_cancel", args=[self.data.standup.pk]),
            {})
        self.assertEqual(EventOccurrence.objects.count(), 0)
