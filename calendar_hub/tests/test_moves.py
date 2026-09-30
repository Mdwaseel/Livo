"""The write side of every source: `move()` called directly.

`test_views.py` drives the drag endpoint and proves the *refusals* — no
permission, a financial document, a deadline dragged behind its own start. This
file covers the other half, which is easy to leave untested precisely because it
is the boring case: the successful reschedule, the no-op when nothing changed,
and the record that vanished between the page rendering and the drop landing.

Called at the source rather than through HTTP, so a failure names the adapter
rather than the endpoint.
"""
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.test import TestCase

from calendar_hub import sources
from calendar_hub.events import CalendarQuery
from calendar_hub.models import CalendarEvent
from core.models import ActivityLog
from documents.models import Document
from projects.models import Milestone, Project

from .factories import (FRIDAY, GRID_END, GRID_START, MONDAY, THURSDAY,
                        TUESDAY, WEDNESDAY, Scenario, forget_perms, set_perm)


def source(key):
    return sources.get_source(key)


class TaskMoveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_successful_move_writes_the_date_and_an_audit_entry(self):
        task, message = source("task").move(
            self.data.manager, self.data.task.pk, THURSDAY)
        task.refresh_from_db()
        self.assertEqual(task.due_date, THURSDAY)
        self.assertIn("moved to", message)
        self.assertTrue(ActivityLog.objects.filter(
            verb="rescheduled task").exists())

    def test_moving_to_the_same_day_changes_nothing_and_says_nothing(self):
        """A drag that lands where it started is not an event worth logging or
        announcing."""
        before = ActivityLog.objects.count()
        _, message = source("task").move(
            self.data.manager, self.data.task.pk, MONDAY)
        self.assertEqual(message, "")
        self.assertEqual(ActivityLog.objects.count(), before)

    def test_a_task_deleted_mid_drag_is_a_lookup_error(self):
        pk = self.data.task.pk
        self.data.task.delete()
        with self.assertRaises(LookupError):
            source("task").move(self.data.manager, pk, THURSDAY)

    def test_without_the_task_modules_edit_right_the_move_is_refused(self):
        set_perm("Developer", "tasks", "edit", False)
        forget_perms(self.data.anna)
        with self.assertRaises(PermissionDenied):
            source("task").move(self.data.anna, self.data.task.pk, THURSDAY)
        self.data.task.refresh_from_db()
        self.assertEqual(self.data.task.due_date, MONDAY)


class MilestoneMoveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_successful_move(self):
        milestone, message = source("milestone").move(
            self.data.manager, self.data.milestone.pk, FRIDAY)
        milestone.refresh_from_db()
        self.assertEqual(milestone.due_date, FRIDAY)
        self.assertIn("moved to", message)

    def test_moving_a_milestone_never_touches_its_completion_date(self):
        """The due date moving says nothing about whether the work is done, and
        `completed_on` is that model's own business."""
        self.data.milestone.apply_status(Milestone.Status.DONE)
        self.data.milestone.save()
        self.data.milestone.refresh_from_db()
        completed = self.data.milestone.completed_on
        self.assertIsNotNone(completed)

        source("milestone").move(self.data.manager, self.data.milestone.pk,
                                 FRIDAY)
        self.data.milestone.refresh_from_db()
        self.assertEqual(self.data.milestone.completed_on, completed)
        self.assertEqual(self.data.milestone.status, Milestone.Status.DONE)

    def test_a_no_op_move(self):
        _, message = source("milestone").move(
            self.data.manager, self.data.milestone.pk, WEDNESDAY)
        self.assertEqual(message, "")

    def test_a_missing_milestone(self):
        with self.assertRaises(LookupError):
            source("milestone").move(self.data.manager, 999999, FRIDAY)


class ProjectMoveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_successful_move(self):
        project, message = source("project").move(
            self.data.manager, self.data.project.pk, FRIDAY + timedelta(days=7))
        project.refresh_from_db()
        self.assertEqual(project.target_end_date, FRIDAY + timedelta(days=7))
        self.assertIn("now due", message)
        self.assertTrue(ActivityLog.objects.filter(
            verb="rescheduled project deadline").exists())

    def test_a_deadline_behind_the_start_date_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            source("project").move(self.data.manager, self.data.project.pk,
                                   self.data.project.start_date -
                                   timedelta(days=1))
        self.assertIn("starts on", str(caught.exception))
        self.data.project.refresh_from_db()
        self.assertEqual(self.data.project.target_end_date, FRIDAY)

    def test_a_project_with_no_start_date_has_nothing_to_be_behind(self):
        self.data.project.start_date = None
        self.data.project.save()
        project, _ = source("project").move(
            self.data.manager, self.data.project.pk, MONDAY)
        project.refresh_from_db()
        self.assertEqual(project.target_end_date, MONDAY)

    def test_a_no_op_move(self):
        _, message = source("project").move(
            self.data.manager, self.data.project.pk, FRIDAY)
        self.assertEqual(message, "")

    def test_a_missing_project(self):
        with self.assertRaises(LookupError):
            source("project").move(self.data.manager, 999999, FRIDAY)


class DocumentMoveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_successful_move(self):
        document, message = source("document").move(
            self.data.manager, self.data.document.pk, FRIDAY)
        document.refresh_from_db()
        self.assertEqual(document.review_due_date, FRIDAY)
        self.assertIn("review moved to", message)

    def test_a_no_op_move(self):
        _, message = source("document").move(
            self.data.manager, self.data.document.pk, THURSDAY)
        self.assertEqual(message, "")

    def test_an_archived_document_is_gone_as_far_as_the_calendar_is_concerned(self):
        self.data.document.is_archived = True
        self.data.document.save()
        with self.assertRaises(LookupError):
            source("document").move(self.data.manager, self.data.document.pk,
                                    FRIDAY)

    def test_a_financial_document_is_invisible_rather_than_forbidden(self):
        """The second gate, isolated.

        `can_move` only proves the viewer may edit documents *in general*; it
        says nothing about whether they may see this particular financial one.
        So the actor here is given `documents.edit` and nothing else — no
        `finance.view`, no `invoices.view` — which is the only combination that
        reaches the check. Every stock role either lacks the edit right (and is
        stopped a line earlier) or can see invoices anyway.

        The refusal is a LookupError rather than a PermissionDenied on purpose:
        confirming the invoice exists is itself the leak.
        """
        set_perm("Developer", "documents", "edit", True)
        forget_perms(self.data.anna)

        with self.assertRaises(LookupError):
            source("document").move(self.data.anna, self.data.invoice.pk,
                                    FRIDAY)
        self.data.invoice.refresh_from_db()
        self.assertEqual(self.data.invoice.review_due_date, THURSDAY)

        # The same person moves a non-financial document without complaint, so
        # the refusal above is about the invoice, not about them.
        moved, _ = source("document").move(
            self.data.anna, self.data.document.pk, FRIDAY)
        moved.refresh_from_db()
        self.assertEqual(moved.review_due_date, FRIDAY)


class EventMoveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_no_op_move_on_a_one_off_event(self):
        _, message = source("event").move(
            self.data.manager, self.data.meeting.pk, TUESDAY)
        self.assertEqual(message, "")

    def test_a_missing_event(self):
        with self.assertRaises(LookupError):
            source("event").move(self.data.manager, 999999, FRIDAY)

    def test_a_private_event_is_invisible_to_a_non_attendee(self):
        private = CalendarEvent.objects.create(
            title="1:1", start_date=TUESDAY, created_by=self.data.pm,
            visibility=CalendarEvent.Visibility.PRIVATE)
        with self.assertRaises(LookupError):
            source("event").move(self.data.bilal, private.pk, FRIDAY)

    def test_moving_an_occurrence_notifies_the_attendees(self):
        from core.models import Notification

        source("event").move(self.data.manager, self.data.standup.pk,
                             TUESDAY, occurrence_date=MONDAY)
        told = set(Notification.objects.values_list("user__username", flat=True))
        self.assertIn("anna", told)
        self.assertIn("bilal", told)
        # The person who did it doesn't need telling.
        self.assertNotIn("mgr", told)


class UnexercisedFilterTests(TestCase):
    """Filter branches each source owns but that the behavioural suites reach
    only through the sources that support every dimension."""

    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def query(self, viewer, **kwargs):
        kwargs.setdefault("start", GRID_START)
        kwargs.setdefault("end", GRID_END)
        return CalendarQuery(viewer=viewer, **kwargs)

    def fetch(self, key, **kwargs):
        return list(source(key).fetch(self.query(self.data.manager, **kwargs)))

    def test_a_project_deadline_can_be_scoped_to_one_project(self):
        self.assertEqual(len(self.fetch("project",
                                        project_id=self.data.project.pk)), 1)
        self.assertEqual(self.fetch("project",
                                    project_id=self.data.other_project.pk), [])

    def test_a_project_deadline_can_be_scoped_to_one_client(self):
        self.assertEqual(len(self.fetch("project",
                                        client_id=self.data.client.pk)), 1)
        self.assertEqual(self.fetch("project",
                                    client_id=self.data.other_client.pk), [])

    def test_a_milestone_can_be_scoped_to_one_project_and_client(self):
        self.assertEqual(len(self.fetch("milestone",
                                        project_id=self.data.project.pk)), 1)
        self.assertEqual(len(self.fetch("milestone",
                                        client_id=self.data.client.pk)), 1)

    def test_a_document_review_can_be_scoped_to_one_project_and_client(self):
        self.assertTrue(self.fetch("document", project_id=self.data.project.pk))
        self.assertTrue(self.fetch("document", client_id=self.data.client.pk))
        self.assertEqual(
            self.fetch("document", project_id=self.data.other_project.pk), [])

    def test_leave_can_be_scoped_to_a_department(self):
        events = self.fetch("leave", department_id=self.data.design.pk)
        kinds = {event.kind for event in events}
        self.assertIn("leave", kinds)     # Bilal is in Design
        self.assertIn("holiday", kinds)   # company-wide, applies to everyone

        others = self.fetch("leave", department_id=self.data.delivery.pk)
        self.assertEqual({event.kind for event in others}, {"holiday"})

    def test_leave_can_be_searched_by_note_and_by_name(self):
        by_note = self.fetch("leave", search="Founders")
        self.assertEqual([event.kind for event in by_note], ["holiday"])
        by_name = self.fetch("leave", search="Bilal")
        self.assertTrue(any(event.kind == "leave" for event in by_name))

    def test_a_birthday_can_be_scoped_to_a_department(self):
        self.assertTrue(self.fetch("birthday",
                                   department_id=self.data.delivery.pk))
        self.assertEqual(
            self.fetch("birthday", department_id=self.data.design.pk), [])

    def test_a_project_deadline_can_be_searched_by_client_name(self):
        self.assertTrue(self.fetch("project", search="Acme"))

    def test_a_milestone_can_be_searched(self):
        self.assertTrue(self.fetch("milestone", search="signed off"))

    def test_a_document_can_be_searched_by_title(self):
        self.assertTrue(self.fetch("document", search="SRS"))
