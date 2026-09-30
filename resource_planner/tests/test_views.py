"""View, RBAC and drag-and-drop tests."""
import json
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import Module, Role, RolePermission
from core.models import ActivityLog, Notification
from projects.models import Task
from resource_planner.models import CapacityProfile, LeaveRecord

from .factories import FRIDAY, MONDAY, THURSDAY, WEDNESDAY, Scenario

PAGES = ("overview", "employees", "departments", "projects", "weekly",
         "monthly", "leave")


def set_perm(role_name, module_key, action, value):
    role = Role.objects.get(name=role_name)
    module, _ = Module.objects.get_or_create(
        key=module_key, defaults={"label": module_key})
    permission, _ = RolePermission.objects.get_or_create(role=role, module=module)
    setattr(permission, f"can_{action}", value)
    permission.save()


class AccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(reverse("resource_planner:overview"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_manager_can_open_every_page(self):
        self.client.force_login(self.data.manager)
        for name in PAGES:
            with self.subTest(page=name):
                self.assertEqual(
                    self.client.get(reverse(f"resource_planner:{name}")).status_code,
                    200)

    def test_project_manager_can_open_every_page(self):
        self.client.force_login(self.data.pm)
        for name in PAGES:
            with self.subTest(page=name):
                self.assertEqual(
                    self.client.get(reverse(f"resource_planner:{name}")).status_code,
                    200)

    def test_developer_is_refused(self):
        """Developers hold no resource_planning permission in the seeded matrix."""
        self.client.force_login(self.data.anna)
        response = self.client.get(reverse("resource_planner:overview"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("core:dashboard"), response["Location"])

    def test_sales_is_refused(self):
        self.client.force_login(self.data.outsider)
        self.assertEqual(
            self.client.get(reverse("resource_planner:employees")).status_code, 302)

    def test_sidebar_link_hidden_without_permission(self):
        self.client.force_login(self.data.anna)
        body = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertNotIn('href="/planning/"', body)

    def test_sidebar_link_shown_with_permission(self):
        self.client.force_login(self.data.manager)
        body = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertIn('href="/planning/"', body)

    def test_granting_the_module_opens_access_without_a_deploy(self):
        """The whole point of putting the gate in the RBAC matrix."""
        set_perm("Developer", "resource_planning", "view", True)
        self.client.force_login(self.data.anna)
        self.assertEqual(
            self.client.get(reverse("resource_planner:overview")).status_code, 200)


class CapacityEditTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def url(self, user=None):
        return reverse("resource_planner:capacity_edit",
                       args=[(user or self.data.anna).pk])

    def test_manager_can_open_the_form(self):
        self.client.force_login(self.data.manager)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hours per working day")

    def test_view_only_user_cannot_edit_capacity(self):
        set_perm("Developer", "resource_planning", "view", True)
        set_perm("Developer", "resource_planning", "edit", False)
        self.client.force_login(self.data.anna)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("resource_planner:employees"), response["Location"])

    def test_saving_creates_a_profile(self):
        self.client.force_login(self.data.manager)
        self.client.post(self.url(), {
            "daily_hours": "6.5", "working_days": ["1", "2", "3", "4"],
            "notes": "4-day week",
        })
        profile = CapacityProfile.objects.get(user=self.data.anna)
        self.assertEqual(profile.daily_hours, Decimal("6.5"))
        self.assertEqual(profile.working_days, "1234")
        self.assertEqual(profile.updated_by, self.data.manager)

    def test_saving_changes_the_planner_numbers(self):
        self.client.force_login(self.data.manager)
        self.client.post(self.url(), {
            "daily_hours": "4", "working_days": ["1", "2", "3", "4", "5"]})
        response = self.client.get(
            reverse("resource_planner:employees") + f"?date={MONDAY}")
        rows = {row["user"].username: row for row in response.context["rows"]}
        self.assertEqual(rows["anna"]["capacity"], Decimal("20.00"))

    def test_rubbish_hours_are_rejected_with_a_message(self):
        self.client.force_login(self.data.manager)
        response = self.client.post(self.url(), {
            "daily_hours": "not a number", "working_days": ["1"]}, follow=True)
        self.assertContains(response, "Enter hours as a number")
        self.assertFalse(CapacityProfile.objects.filter(user=self.data.anna).exists())

    def test_more_than_24_hours_a_day_is_rejected(self):
        self.client.force_login(self.data.manager)
        response = self.client.post(self.url(), {
            "daily_hours": "30", "working_days": ["1"]}, follow=True)
        self.assertContains(response, "only 24 hours")

    def test_no_working_days_is_rejected(self):
        self.client.force_login(self.data.manager)
        response = self.client.post(self.url(), {
            "daily_hours": "8", "working_days": []}, follow=True)
        self.assertContains(response, "at least one working day")

    def test_edit_is_logged(self):
        self.client.force_login(self.data.manager)
        self.client.post(self.url(), {"daily_hours": "7", "working_days": ["1"]})
        self.assertTrue(
            ActivityLog.objects.filter(verb="updated capacity").exists())


class LeaveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_manager_can_record_approved_leave(self):
        self.client.force_login(self.data.manager)
        self.client.post(reverse("resource_planner:leave_create"), {
            "user": self.data.anna.pk, "kind": "VACATION",
            "start_date": str(MONDAY), "end_date": str(FRIDAY)})
        record = LeaveRecord.objects.get(user=self.data.anna)
        self.assertEqual(record.status, LeaveRecord.Status.APPROVED)
        self.assertEqual(record.approved_by, self.data.manager)

    def test_a_recorder_without_approval_rights_creates_a_request(self):
        """Otherwise anyone who can type could zero out their own capacity."""
        set_perm("Project Manager", "leaves", "create", True)
        set_perm("Project Manager", "leaves", "approve", False)
        self.client.force_login(self.data.pm)
        self.client.post(reverse("resource_planner:leave_create"), {
            "user": self.data.anna.pk, "kind": "VACATION",
            "start_date": str(MONDAY), "end_date": str(FRIDAY)})
        record = LeaveRecord.objects.get(user=self.data.anna)
        self.assertEqual(record.status, LeaveRecord.Status.REQUESTED)

    def test_a_request_does_not_change_capacity_until_approved(self):
        self.client.force_login(self.data.manager)
        LeaveRecord.objects.create(
            user=self.data.anna, status=LeaveRecord.Status.REQUESTED,
            start_date=MONDAY, end_date=FRIDAY)
        response = self.client.get(
            reverse("resource_planner:employees") + f"?date={MONDAY}")
        rows = {row["user"].username: row for row in response.context["rows"]}
        self.assertEqual(rows["anna"]["capacity"], Decimal("40.00"))

    def test_approving_a_request_removes_the_capacity(self):
        record = LeaveRecord.objects.create(
            user=self.data.anna, status=LeaveRecord.Status.REQUESTED,
            start_date=MONDAY, end_date=FRIDAY)
        self.client.force_login(self.data.manager)
        self.client.post(reverse("resource_planner:leave_update", args=[record.pk]),
                         {"action": "approve"})
        response = self.client.get(
            reverse("resource_planner:employees") + f"?date={MONDAY}")
        rows = {row["user"].username: row for row in response.context["rows"]}
        self.assertEqual(rows["anna"]["capacity"], Decimal("0.00"))

    def test_company_wide_holiday_hits_everyone(self):
        self.client.force_login(self.data.manager)
        self.client.post(reverse("resource_planner:leave_create"), {
            "user": "", "kind": "HOLIDAY",
            "start_date": str(MONDAY), "end_date": str(MONDAY)})
        response = self.client.get(
            reverse("resource_planner:employees") + f"?date={MONDAY}")
        for row in response.context["rows"]:
            if row["user"].username == "bilal":
                self.assertEqual(row["capacity"], Decimal("24.00"))  # 30 − 6
            elif row["user"].username == "anna":
                self.assertEqual(row["capacity"], Decimal("32.00"))  # 40 − 8

    def test_backwards_dates_are_rejected(self):
        self.client.force_login(self.data.manager)
        response = self.client.post(reverse("resource_planner:leave_create"), {
            "user": self.data.anna.pk, "start_date": str(FRIDAY),
            "end_date": str(MONDAY)}, follow=True)
        self.assertContains(response, "can&#x27;t be before the start")

    def test_missing_dates_are_rejected(self):
        self.client.force_login(self.data.manager)
        response = self.client.post(reverse("resource_planner:leave_create"),
                                    {"user": self.data.anna.pk}, follow=True)
        self.assertContains(response, "Both dates are required")

    def test_user_without_leave_rights_cannot_record(self):
        set_perm("Project Manager", "leaves", "create", False)
        set_perm("Project Manager", "leaves", "edit", False)
        self.client.force_login(self.data.pm)
        before = LeaveRecord.objects.count()
        self.client.post(reverse("resource_planner:leave_create"), {
            "user": self.data.anna.pk, "start_date": str(MONDAY),
            "end_date": str(FRIDAY)})
        self.assertEqual(LeaveRecord.objects.count(), before)

    def test_deleting_leave_restores_capacity(self):
        self.client.force_login(self.data.manager)
        self.client.post(
            reverse("resource_planner:leave_update", args=[self.data.carla_leave.pk]),
            {"action": "delete"})
        response = self.client.get(
            reverse("resource_planner:employees") + f"?date={MONDAY}")
        rows = {row["user"].username: row for row in response.context["rows"]}
        self.assertEqual(rows["carla"]["capacity"], Decimal("40.00"))


class ReassignTests(TestCase):
    """Drag and drop."""

    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def post(self, payload):
        return self.client.post(reverse("resource_planner:reassign"),
                                data=json.dumps(payload),
                                content_type="application/json")

    def test_manager_can_reassign(self):
        self.client.force_login(self.data.manager)
        response = self.post({"task": self.data.backlog_task.pk,
                              "assignee": self.data.anna.pk})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.data.backlog_task.refresh_from_db()
        self.assertEqual(self.data.backlog_task.assignee, self.data.anna)

    def test_dropping_on_a_day_moves_the_due_date_too(self):
        self.client.force_login(self.data.manager)
        self.post({"task": self.data.backlog_task.pk,
                   "assignee": self.data.anna.pk, "due_date": str(WEDNESDAY)})
        self.data.backlog_task.refresh_from_db()
        self.assertEqual(self.data.backlog_task.due_date, WEDNESDAY)

    def test_a_null_assignee_sends_the_task_back_to_the_backlog(self):
        self.client.force_login(self.data.manager)
        self.post({"task": self.data.anna_task.pk, "assignee": None})
        self.data.anna_task.refresh_from_db()
        self.assertIsNone(self.data.anna_task.assignee)

    def test_reassignment_is_refused_without_tasks_assign(self):
        """The planner must not become a way around the task permission."""
        set_perm("Project Manager", "tasks", "assign", False)
        self.client.force_login(self.data.pm)
        response = self.post({"task": self.data.backlog_task.pk,
                              "assignee": self.data.anna.pk})
        self.assertEqual(response.status_code, 403)
        self.data.backlog_task.refresh_from_db()
        self.assertIsNone(self.data.backlog_task.assignee)

    def test_anonymous_cannot_reassign(self):
        response = self.post({"task": self.data.backlog_task.pk,
                              "assignee": self.data.anna.pk})
        self.assertEqual(response.status_code, 302)

    def test_get_is_not_allowed(self):
        self.client.force_login(self.data.manager)
        self.assertEqual(
            self.client.get(reverse("resource_planner:reassign")).status_code, 405)

    def test_unknown_task_is_a_404(self):
        self.client.force_login(self.data.manager)
        self.assertEqual(self.post({"task": 999999,
                                    "assignee": self.data.anna.pk}).status_code, 404)

    def test_malformed_json_is_a_400(self):
        self.client.force_login(self.data.manager)
        response = self.client.post(reverse("resource_planner:reassign"),
                                    data="{not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_inactive_assignee_is_rejected(self):
        self.data.carla.is_active = False
        self.data.carla.save()
        self.client.force_login(self.data.manager)
        response = self.post({"task": self.data.backlog_task.pk,
                              "assignee": self.data.carla.pk})
        self.assertEqual(response.status_code, 400)

    def test_invalid_date_is_rejected(self):
        self.client.force_login(self.data.manager)
        response = self.post({"task": self.data.backlog_task.pk,
                              "assignee": self.data.anna.pk,
                              "due_date": "not-a-date"})
        self.assertEqual(response.status_code, 400)

    def test_a_no_op_move_reports_unchanged(self):
        self.client.force_login(self.data.manager)
        response = self.post({"task": self.data.anna_task.pk,
                              "assignee": self.data.anna.pk})
        self.assertTrue(response.json()["unchanged"])

    def test_overbooking_warns_but_still_saves(self):
        """A manager reassigning into an overload usually knows something the
        estimate doesn't; refusing the drop just gets worked around."""
        self.client.force_login(self.data.manager)
        heavy = Task.objects.create(
            project=self.data.project, title="Monster", due_date=THURSDAY,
            estimated_hours=Decimal("60"))
        response = self.post({"task": heavy.pk, "assignee": self.data.anna.pk,
                              "due_date": str(THURSDAY)})
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("over capacity", payload["warning"])
        heavy.refresh_from_db()
        self.assertEqual(heavy.assignee, self.data.anna)

    def test_a_comfortable_move_carries_no_warning(self):
        self.client.force_login(self.data.manager)
        response = self.post({"task": self.data.backlog_task.pk,
                              "assignee": self.data.anna.pk,
                              "due_date": str(WEDNESDAY)})
        self.assertEqual(response.json()["warning"], "")

    def test_reassignment_is_logged(self):
        self.client.force_login(self.data.manager)
        self.post({"task": self.data.backlog_task.pk,
                   "assignee": self.data.anna.pk})
        self.assertTrue(ActivityLog.objects.filter(verb="reassigned task").exists())

    def test_the_new_assignee_is_notified(self):
        self.client.force_login(self.data.manager)
        self.post({"task": self.data.backlog_task.pk,
                   "assignee": self.data.anna.pk})
        self.assertTrue(Notification.objects.filter(user=self.data.anna).exists())

    def test_assigning_to_yourself_sends_no_notification(self):
        set_perm("Manager", "tasks", "assign", True)
        self.client.force_login(self.data.manager)
        self.post({"task": self.data.backlog_task.pk,
                   "assignee": self.data.manager.pk})
        self.assertFalse(Notification.objects.filter(user=self.data.manager).exists())


class ContentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        self.client.force_login(self.data.manager)

    def get(self, name, **params):
        query = "&".join(f"{key}={value}" for key, value in params.items())
        return self.client.get(reverse(f"resource_planner:{name}") + f"?{query}")

    def test_overview_shows_the_three_indicator_states(self):
        response = self.get("overview", date=MONDAY)
        body = response.content.decode()
        self.assertIn("Can take more", body)
        self.assertIn("Near capacity", body)
        self.assertIn("Overloaded", body)

    def test_overbooking_banner_names_the_person(self):
        response = self.get("overview", date=MONDAY)
        self.assertContains(response, "Overbooking detected")
        self.assertContains(response, "Bilal")

    def test_weekly_grid_has_seven_columns(self):
        response = self.get("weekly", date=MONDAY)
        self.assertEqual(len(response.context["grid"]["days"]), 7)

    def test_monthly_grid_spans_the_month(self):
        response = self.get("monthly", date=MONDAY)
        self.assertEqual(len(response.context["grid"]["days"]), 31)

    def test_drag_handles_present_for_assigners(self):
        response = self.get("weekly", date=MONDAY)
        self.assertContains(response, 'draggable="true"')
        self.assertContains(response, 'data-drop="1"')

    def test_drag_handles_absent_without_assign_permission(self):
        set_perm("Project Manager", "tasks", "assign", False)
        self.client.force_login(self.data.pm)
        response = self.get("weekly", date=MONDAY)
        self.assertNotContains(response, 'draggable="true"')

    def test_filters_narrow_the_page(self):
        response = self.get("employees", date=MONDAY,
                            department=self.data.design.pk)
        names = {row["user"].username for row in response.context["rows"]}
        self.assertEqual(names, {"bilal", "carla"})

    def test_period_navigation_moves_a_week(self):
        response = self.get("weekly", date=MONDAY)
        window = response.context["window"]
        self.assertEqual(window.next, MONDAY + timedelta(days=7))
        self.assertEqual(window.previous, MONDAY - timedelta(days=7))

    def test_forecast_is_eight_weeks(self):
        response = self.get("overview", date=MONDAY)
        self.assertEqual(len(response.context["forecast"]), 8)

    def test_pages_survive_an_empty_database(self):
        Task.objects.all().delete()
        LeaveRecord.objects.all().delete()
        CapacityProfile.objects.all().delete()
        for name in PAGES:
            with self.subTest(page=name):
                self.assertEqual(self.get(name).status_code, 200)

    def test_garbage_query_parameters_do_not_crash(self):
        for params in ({"date": "nonsense"}, {"employee": "abc"},
                       {"department": "-1"}, {"project": "0"}):
            with self.subTest(params=params):
                self.assertEqual(self.get("weekly", **params).status_code, 200)
