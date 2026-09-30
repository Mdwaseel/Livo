"""Edge paths: unscoped selectors, permission fall-throughs, and the error
branches of the leave and capacity editors.

These are the routes a normal click never takes — a hand-typed URL, a role
edited mid-session, an unfiltered call from a future caller — so they get their
own file rather than cluttering the behavioural suites.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from projects.models import Task
from resource_planner import permissions, selectors
from resource_planner.models import CapacityProfile, LeaveRecord

from .factories import FRIDAY, MONDAY, SUNDAY, Scenario
from .test_views import set_perm


class UnscopedSelectorTests(TestCase):
    """Every selector takes an optional scope; the unscoped path is the one a
    future caller will reach for first."""

    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_leave_records_without_a_user_list_returns_everything(self):
        records = selectors.leave_records(MONDAY, SUNDAY)
        self.assertIn(self.data.carla_leave, records)

    def test_leave_records_always_include_company_wide_rows(self):
        holiday = LeaveRecord.objects.create(
            kind=LeaveRecord.Kind.HOLIDAY, status=LeaveRecord.Status.APPROVED,
            start_date=MONDAY, end_date=MONDAY)
        # Scoped to one person, the company holiday must still come back.
        records = selectors.leave_records(MONDAY, SUNDAY,
                                          user_ids=[self.data.anna.pk])
        self.assertIn(holiday, records)

    def test_rejected_and_cancelled_leave_never_loads(self):
        LeaveRecord.objects.create(
            user=self.data.anna, status=LeaveRecord.Status.REJECTED,
            start_date=MONDAY, end_date=FRIDAY)
        LeaveRecord.objects.create(
            user=self.data.anna, status=LeaveRecord.Status.CANCELLED,
            start_date=MONDAY, end_date=FRIDAY)
        statuses = {record.status
                    for record in selectors.leave_records(MONDAY, SUNDAY)}
        self.assertNotIn(LeaveRecord.Status.REJECTED, statuses)
        self.assertNotIn(LeaveRecord.Status.CANCELLED, statuses)

    def test_logged_hours_without_a_user_list(self):
        totals = selectors.logged_hours_by_user(MONDAY, SUNDAY)
        self.assertEqual(totals[self.data.anna.pk], Decimal("10"))

    def test_logged_hours_can_be_scoped_to_one_project(self):
        totals = selectors.logged_hours_by_user(
            MONDAY, SUNDAY, project_id=self.data.project.pk)
        self.assertEqual(totals[self.data.anna.pk], Decimal("10"))

    def test_logged_hours_by_day_without_a_user_list(self):
        cells = selectors.logged_hours_by_user_day(MONDAY, SUNDAY)
        self.assertEqual(cells[(self.data.anna.pk, MONDAY)], Decimal("8"))

    def test_overdue_hours_without_a_user_list(self):
        Task.objects.create(project=self.data.project, title="Late",
                            assignee=self.data.anna,
                            due_date=MONDAY - timedelta(days=5),
                            estimated_hours=Decimal("9"))
        self.assertEqual(
            selectors.overdue_hours_by_user(MONDAY)[self.data.anna.pk],
            Decimal("9"))

    def test_sprint_hours_without_a_user_list(self):
        totals = selectors.sprint_hours_by_user(MONDAY)
        self.assertEqual(totals[self.data.bilal.pk], Decimal("40"))

    def test_sprint_with_open_ended_dates_still_counts(self):
        """A sprint with no start or end is running; treating null as
        out-of-range would silently drop its hours."""
        self.data.sprint.start_date = None
        self.data.sprint.end_date = None
        self.data.sprint.save()
        self.assertEqual(
            selectors.sprint_hours_by_user(MONDAY)[self.data.anna.pk],
            Decimal("4"))

    def test_open_tasks_can_be_scoped_by_department(self):
        tasks = selectors.open_tasks(department_id=self.data.design.pk)
        self.assertEqual(list(tasks), [self.data.bilal_task])

    def test_unassigned_backlog_can_be_scoped_to_a_project(self):
        tasks = selectors.unassigned_open_tasks(project_id=self.data.project.pk)
        self.assertIn(self.data.backlog_task, tasks)

    def test_task_counts_without_a_user_list(self):
        counts = selectors.task_counts_by_user()
        self.assertEqual(counts[self.data.anna.pk]["open_count"], 1)

    def test_project_allocation_can_be_scoped_to_one_project(self):
        rows = selectors.project_allocation_rows(
            MONDAY, SUNDAY, project_id=self.data.project.pk)
        self.assertEqual(len(rows), 1)


class PermissionHelperTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_helpers_agree_with_the_matrix(self):
        self.assertTrue(permissions.can_view(self.data.manager))
        self.assertTrue(permissions.can_edit_capacity(self.data.manager))
        self.assertFalse(permissions.can_view(self.data.anna))

    def test_capacity_editor_refuses_someone_who_lost_view_access(self):
        """A role edited mid-session: the outer view gate must fire before the
        edit gate, not after."""
        set_perm("Project Manager", "resource_planning", "view", False)
        set_perm("Project Manager", "resource_planning", "edit", False)
        self.client.force_login(self.data.pm)
        response = self.client.get(
            reverse("resource_planner:capacity_edit", args=[self.data.anna.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("core:dashboard"), response["Location"])


class EditorErrorPathTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        self.client.force_login(self.data.manager)

    def test_capacity_form_rejects_a_bad_override(self):
        response = self.client.post(
            reverse("resource_planner:capacity_edit", args=[self.data.anna.pk]),
            {"daily_hours": "8", "working_days": ["1"],
             "weekly_hours_override": "rubbish"}, follow=True)
        self.assertContains(response, "Enter hours as a number")

    def test_capacity_form_rejects_negative_hours(self):
        response = self.client.post(
            reverse("resource_planner:capacity_edit", args=[self.data.anna.pk]),
            {"daily_hours": "-3", "working_days": ["1"]}, follow=True)
        self.assertContains(response, "negative")

    def test_blank_overrides_are_stored_as_null_not_zero(self):
        """Zero would mean no capacity; blank means derive it."""
        self.client.post(
            reverse("resource_planner:capacity_edit", args=[self.data.anna.pk]),
            {"daily_hours": "8", "working_days": ["1", "2"],
             "weekly_hours_override": "", "monthly_hours_override": ""})
        profile = CapacityProfile.objects.get(user=self.data.anna)
        self.assertIsNone(profile.weekly_hours_override)
        self.assertEqual(profile.weekly_hours, Decimal("16"))

    def test_an_unknown_leave_kind_falls_back_to_other(self):
        self.client.post(reverse("resource_planner:leave_create"), {
            "user": self.data.anna.pk, "kind": "SABBATICAL_ON_MARS",
            "start_date": str(MONDAY), "end_date": str(FRIDAY)})
        record = LeaveRecord.objects.get(user=self.data.anna)
        self.assertEqual(record.kind, LeaveRecord.Kind.OTHER)

    def test_rejecting_a_request_leaves_capacity_untouched(self):
        record = LeaveRecord.objects.create(
            user=self.data.anna, status=LeaveRecord.Status.REQUESTED,
            start_date=MONDAY, end_date=FRIDAY)
        self.client.post(
            reverse("resource_planner:leave_update", args=[record.pk]),
            {"action": "reject"})
        record.refresh_from_db()
        self.assertEqual(record.status, LeaveRecord.Status.REJECTED)
        response = self.client.get(
            reverse("resource_planner:employees") + f"?date={MONDAY}")
        rows = {row["user"].username: row for row in response.context["rows"]}
        self.assertEqual(rows["anna"]["capacity"], Decimal("40.00"))

    def test_an_unknown_action_is_a_harmless_no_op(self):
        record = LeaveRecord.objects.create(
            user=self.data.anna, status=LeaveRecord.Status.REQUESTED,
            start_date=MONDAY, end_date=FRIDAY)
        self.client.post(
            reverse("resource_planner:leave_update", args=[record.pk]),
            {"action": "explode"})
        record.refresh_from_db()
        self.assertEqual(record.status, LeaveRecord.Status.REQUESTED)

    def test_approving_without_the_permission_is_refused(self):
        set_perm("Project Manager", "leaves", "approve", False)
        set_perm("Project Manager", "leaves", "create", True)
        record = LeaveRecord.objects.create(
            user=self.data.anna, status=LeaveRecord.Status.REQUESTED,
            start_date=MONDAY, end_date=FRIDAY)
        self.client.force_login(self.data.pm)
        self.client.post(
            reverse("resource_planner:leave_update", args=[record.pk]),
            {"action": "approve"})
        record.refresh_from_db()
        self.assertEqual(record.status, LeaveRecord.Status.REQUESTED)

    def test_deleting_without_the_permission_is_refused(self):
        set_perm("Project Manager", "leaves", "create", False)
        set_perm("Project Manager", "leaves", "edit", False)
        self.client.force_login(self.data.pm)
        self.client.post(
            reverse("resource_planner:leave_update",
                    args=[self.data.carla_leave.pk]),
            {"action": "delete"})
        self.assertTrue(
            LeaveRecord.objects.filter(pk=self.data.carla_leave.pk).exists())
