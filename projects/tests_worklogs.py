"""Work logs / timesheets: hours derivation, task rollup, approval workflow,
rollup helpers, and the timesheet pages."""
from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import Client
from core.models import Notification
from .models import Project, Task, WorkLogEntry

User = get_user_model()


class WorkLogBase(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(name="Hours Co")
        self.project = Project.objects.create(client=self.client_obj, name="Site")
        # PM → Project Manager role, which holds worklogs RWD (no approve).
        self.manager = User.objects.create_user("mgr", password="x", role="PM",
                                                first_name="Mia")
        # DEV → Developer role: worklogs RWD, reports to the manager.
        self.dev = User.objects.create_user("dev", password="x", role="DEV",
                                            first_name="Dana",
                                            reports_to=self.manager)
        # OWNER → Super Admin, so it holds worklogs.approve outright.
        self.boss = User.objects.create_user("boss", password="x", role="OWNER",
                                             first_name="Bo")
        # Membership is what makes a project visible to a non-manager now.
        self.project.members.add(self.manager, self.dev, self.boss)
        self.today = timezone.localdate()

    def make_task(self, **kwargs):
        kwargs.setdefault("title", "Build homepage")
        return Task.objects.create(project=self.project, **kwargs)

    def make_log(self, **kwargs):
        kwargs.setdefault("project", self.project)
        kwargs.setdefault("date", self.today)
        kwargs.setdefault("description", "Worked on it")
        kwargs.setdefault("logged_by", self.dev)
        return WorkLogEntry.objects.create(**kwargs)


class HoursDerivationTests(WorkLogBase):
    def test_start_and_end_fill_in_blank_hours(self):
        entry = self.make_log(start_time=time(9, 0), end_time=time(12, 30))
        self.assertEqual(entry.hours, Decimal("3.50"))

    def test_explicit_hours_win_over_times(self):
        entry = self.make_log(start_time=time(9, 0), end_time=time(17, 0),
                              hours=Decimal("2"))
        self.assertEqual(entry.hours, Decimal("2"))

    def test_times_spanning_midnight_are_not_negative(self):
        entry = self.make_log(start_time=time(22, 0), end_time=time(1, 30))
        self.assertEqual(entry.hours, Decimal("3.50"))

    def test_quarter_hour_precision(self):
        entry = self.make_log(start_time=time(9, 0), end_time=time(9, 45))
        self.assertEqual(entry.hours, Decimal("0.75"))

    def test_hours_stay_none_without_both_times(self):
        entry = self.make_log(start_time=time(9, 0))
        self.assertIsNone(entry.hours)

    def test_derivation_via_the_create_view(self):
        self.client.login(username="dev", password="x")

        self.client.post(reverse("projects:worklog_create", args=[self.project.pk]), {
            "date": self.today.isoformat(), "description": "Sprint work",
            "category": "DEV", "start_time": "10:00", "end_time": "13:15",
            "hours": "", "is_billable": "1",
        })

        entry = WorkLogEntry.objects.get()
        self.assertEqual(entry.hours, Decimal("3.25"))
        self.assertEqual(entry.logged_by, self.dev)

    def test_view_rejects_a_malformed_time(self):
        """POST times arrive as strings; garbage must be reported, not crash."""
        self.client.login(username="dev", password="x")

        r = self.client.post(
            reverse("projects:worklog_create", args=[self.project.pk]),
            {"date": self.today.isoformat(), "description": "Bad clock",
             "start_time": "not-a-time", "end_time": "13:00", "hours": ""},
            follow=True)

        self.assertEqual(WorkLogEntry.objects.count(), 0)
        self.assertContains(r, "valid times")

    def test_view_rejects_an_entry_with_neither_hours_nor_times(self):
        self.client.login(username="dev", password="x")

        self.client.post(reverse("projects:worklog_create", args=[self.project.pk]), {
            "date": self.today.isoformat(), "description": "Vague", "hours": "",
        })

        self.assertEqual(WorkLogEntry.objects.count(), 0)


class TaskRollupTests(WorkLogBase):
    def test_logging_against_a_task_updates_actual_hours(self):
        task = self.make_task(estimated_hours=Decimal("10"))
        self.client.login(username="dev", password="x")

        self.client.post(reverse("projects:worklog_create", args=[self.project.pk]), {
            "date": self.today.isoformat(), "description": "Built the header",
            "category": "DEV", "task": task.pk, "hours": "3.5", "is_billable": "1",
        })

        task.refresh_from_db()
        self.assertEqual(task.actual_hours, Decimal("3.50"))

    def test_multiple_logs_accumulate(self):
        task = self.make_task()
        self.make_log(task=task, hours=Decimal("2"))
        self.make_log(task=task, hours=Decimal("3.25"))
        task.refresh_from_db()
        self.assertEqual(task.actual_hours, Decimal("5.25"))

    def test_unapproved_hours_still_count(self):
        """Effort must show immediately, not wait on a review round-trip."""
        task = self.make_task()
        self.make_log(task=task, hours=Decimal("4"),
                      approval_status=WorkLogEntry.Approval.SUBMITTED)
        task.refresh_from_db()
        self.assertEqual(task.actual_hours, Decimal("4"))

    def test_deleting_a_log_lowers_actual_hours(self):
        task = self.make_task()
        keep = self.make_log(task=task, hours=Decimal("2"))
        drop = self.make_log(task=task, hours=Decimal("3"))
        task.refresh_from_db()
        self.assertEqual(task.actual_hours, Decimal("5"))

        drop.delete()

        task.refresh_from_db()
        self.assertEqual(task.actual_hours, Decimal("2"))
        self.assertEqual(list(task.work_logs.all()), [keep])

    def test_moving_a_log_between_tasks_resyncs_both(self):
        first = self.make_task(title="First")
        second = self.make_task(title="Second")
        entry = self.make_log(task=first, hours=Decimal("6"))

        entry.task = second
        entry.save()

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.actual_hours, Decimal("0"))
        self.assertEqual(second.actual_hours, Decimal("6"))

    def test_editing_hours_resyncs_the_task(self):
        task = self.make_task()
        entry = self.make_log(task=task, hours=Decimal("2"))
        entry.hours = Decimal("8")
        entry.save()
        task.refresh_from_db()
        self.assertEqual(task.actual_hours, Decimal("8"))

    def test_project_level_logs_touch_no_task(self):
        task = self.make_task()
        self.make_log(hours=Decimal("5"))  # no task
        task.refresh_from_db()
        self.assertEqual(task.actual_hours, Decimal("0"))

    def test_deleting_the_task_keeps_the_log(self):
        """task is SET_NULL — losing a task must not erase the time record."""
        task = self.make_task()
        self.make_log(task=task, hours=Decimal("3"))
        task.delete()
        entry = WorkLogEntry.objects.get()
        self.assertIsNone(entry.task_id)
        self.assertEqual(entry.hours, Decimal("3"))


class EstimateVarianceTests(WorkLogBase):
    def test_overrun_is_flagged(self):
        task = self.make_task(estimated_hours=Decimal("4"))
        self.make_log(task=task, hours=Decimal("6.5"))
        task.refresh_from_db()

        self.assertTrue(task.is_over_estimate)
        self.assertEqual(task.hours_variance, Decimal("2.50"))
        self.assertEqual(task.effort_percent, 100)   # bar caps; colour signals

    def test_under_estimate_is_not_flagged(self):
        task = self.make_task(estimated_hours=Decimal("10"))
        self.make_log(task=task, hours=Decimal("2.5"))
        task.refresh_from_db()

        self.assertFalse(task.is_over_estimate)
        self.assertEqual(task.hours_variance, Decimal("-7.50"))
        self.assertEqual(task.effort_percent, 25)

    def test_no_estimate_means_no_variance(self):
        task = self.make_task()
        self.make_log(task=task, hours=Decimal("9"))
        task.refresh_from_db()
        self.assertIsNone(task.hours_variance)
        self.assertFalse(task.is_over_estimate)

    def test_overrun_renders_on_the_task_page(self):
        task = self.make_task(estimated_hours=Decimal("4"))
        self.make_log(task=task, hours=Decimal("6.5"))
        self.client.login(username="dev", password="x")

        r = self.client.get(task.get_absolute_url())

        self.assertContains(r, "over by")
        self.assertContains(r, "danger-text")

    def test_task_page_lists_its_logs_with_a_total(self):
        task = self.make_task()
        self.make_log(task=task, hours=Decimal("2"), description="Header")
        self.make_log(task=task, hours=Decimal("1.5"), description="Footer",
                      is_billable=False)
        self.client.login(username="dev", password="x")

        r = self.client.get(task.get_absolute_url())

        self.assertContains(r, "Header")
        self.assertContains(r, "Footer")
        self.assertContains(r, "3.50 h total")
        self.assertContains(r, "2 h billable")


class ApprovalWorkflowTests(WorkLogBase):
    def test_submit_week_sends_entries_to_the_manager(self):
        monday = self.today - timedelta(days=self.today.weekday())
        self.make_log(date=monday, hours=Decimal("4"))
        self.make_log(date=monday + timedelta(days=1), hours=Decimal("5"))
        self.make_log(date=monday - timedelta(days=7), hours=Decimal("3"))  # prior week
        self.client.login(username="dev", password="x")

        self.client.post(reverse("worklogs:submit_week"),
                         {"week_start": monday.isoformat()})

        this_week = WorkLogEntry.objects.filter(date__gte=monday)
        self.assertTrue(all(e.is_awaiting_approval for e in this_week))
        prior = WorkLogEntry.objects.get(date=monday - timedelta(days=7))
        self.assertEqual(prior.approval_status, WorkLogEntry.Approval.NONE)

        note = Notification.objects.get(user=self.manager)
        self.assertIn("Dana", note.text)
        self.assertIn("2 work logs", note.text)

    def test_manager_approves_via_reports_to_without_the_perm(self):
        """The PM role has no worklogs.approve — the reporting line is enough."""
        self.assertFalse(self.manager.has_perm("projects.change_worklogentry"))
        entry = self.make_log(hours=Decimal("4"),
                              approval_status=WorkLogEntry.Approval.SUBMITTED)
        self.client.login(username="mgr", password="x")

        self.client.post(reverse("projects:worklog_approve", args=[entry.pk]))

        entry.refresh_from_db()
        self.assertEqual(entry.approval_status, WorkLogEntry.Approval.APPROVED)
        self.assertEqual(entry.approved_by, self.manager)
        self.assertIsNotNone(entry.approved_on)
        self.assertTrue(
            Notification.objects.filter(user=self.dev,
                                        text__startswith="Work log approved").exists())

    def test_reject_requires_a_note_and_notifies(self):
        entry = self.make_log(hours=Decimal("4"),
                              approval_status=WorkLogEntry.Approval.SUBMITTED)
        self.client.login(username="mgr", password="x")

        self.client.post(reverse("projects:worklog_reject", args=[entry.pk]),
                         {"note": "  "})
        entry.refresh_from_db()
        self.assertEqual(entry.approval_status, WorkLogEntry.Approval.SUBMITTED)

        self.client.post(reverse("projects:worklog_reject", args=[entry.pk]),
                         {"note": "Split this across two days"})
        entry.refresh_from_db()
        self.assertEqual(entry.approval_status, WorkLogEntry.Approval.REJECTED)
        self.assertEqual(entry.review_note, "Split this across two days")
        self.assertTrue(
            Notification.objects.filter(user=self.dev,
                                        text__contains="needs changes").exists())

    def test_unrelated_user_cannot_approve(self):
        stranger = User.objects.create_user("stranger", password="x", role="DEV")
        entry = self.make_log(hours=Decimal("4"),
                              approval_status=WorkLogEntry.Approval.SUBMITTED)
        self.client.force_login(stranger)

        self.client.post(reverse("projects:worklog_approve", args=[entry.pk]))

        entry.refresh_from_db()
        self.assertEqual(entry.approval_status, WorkLogEntry.Approval.SUBMITTED)

    def test_editing_an_approved_entry_reopens_it(self):
        entry = self.make_log(hours=Decimal("4"),
                              approval_status=WorkLogEntry.Approval.APPROVED,
                              approved_by=self.manager,
                              approved_on=timezone.now())
        self.client.login(username="dev", password="x")

        self.client.post(reverse("projects:worklog_edit", args=[entry.pk]), {
            "date": self.today.isoformat(), "description": "Corrected",
            "category": "DEV", "hours": "6", "is_billable": "1",
        })

        entry.refresh_from_db()
        self.assertEqual(entry.approval_status, WorkLogEntry.Approval.SUBMITTED)
        self.assertIsNone(entry.approved_by)
        self.assertIsNone(entry.approved_on)
        self.assertEqual(entry.hours, Decimal("6"))

    def test_approved_time_cannot_be_deleted_by_the_logger(self):
        entry = self.make_log(hours=Decimal("4"),
                              approval_status=WorkLogEntry.Approval.APPROVED)
        self.client.login(username="dev", password="x")

        self.client.post(reverse("projects:worklog_delete", args=[entry.pk]))

        self.assertTrue(WorkLogEntry.objects.filter(pk=entry.pk).exists())

    def test_submitting_a_week_with_nothing_pending_is_harmless(self):
        monday = self.today - timedelta(days=self.today.weekday())
        self.client.login(username="dev", password="x")
        r = self.client.post(reverse("worklogs:submit_week"),
                             {"week_start": monday.isoformat()}, follow=True)
        self.assertContains(r, "Nothing to submit")


class OwnershipPermissionTests(WorkLogBase):
    def test_owner_can_edit_own_log_without_the_edit_perm(self):
        nobody = User.objects.create_user("nobody", password="x", role="DEV")
        entry = self.make_log(logged_by=nobody, hours=Decimal("2"))
        self.client.force_login(nobody)

        self.client.post(reverse("projects:worklog_edit", args=[entry.pk]), {
            "date": self.today.isoformat(), "description": "Mine, edited",
            "category": "DEV", "hours": "3",
        })

        entry.refresh_from_db()
        self.assertEqual(entry.description, "Mine, edited")

    def test_editing_someone_elses_log_needs_the_edit_perm(self):
        sales = User.objects.create_user("sales", password="x", role="SALES")
        entry = self.make_log(hours=Decimal("2"), description="Dana's work")
        self.client.force_login(sales)

        self.client.post(reverse("projects:worklog_edit", args=[entry.pk]), {
            "date": self.today.isoformat(), "description": "Hijacked",
            "category": "DEV", "hours": "9",
        })

        entry.refresh_from_db()
        self.assertEqual(entry.description, "Dana's work")

    def test_holder_of_worklogs_edit_can_edit_anyones(self):
        entry = self.make_log(hours=Decimal("2"), description="Dana's work")
        self.client.login(username="mgr", password="x")  # PM has worklogs edit

        self.client.post(reverse("projects:worklog_edit", args=[entry.pk]), {
            "date": self.today.isoformat(), "description": "Corrected by PM",
            "category": "DEV", "hours": "2",
        })

        entry.refresh_from_db()
        self.assertEqual(entry.description, "Corrected by PM")

    def test_module_outsider_cannot_log_time(self):
        outsider = User.objects.create_user("client", password="x")
        outsider.primary_role = None
        outsider.save(update_fields=["primary_role"])
        self.client.force_login(outsider)

        self.client.post(reverse("projects:worklog_create", args=[self.project.pk]), {
            "date": self.today.isoformat(), "description": "Nope", "hours": "3",
        })

        self.assertEqual(WorkLogEntry.objects.count(), 0)


class RollupHelperTests(WorkLogBase):
    def test_user_month_summary_splits_billable(self):
        self.make_log(hours=Decimal("6"), is_billable=True)
        self.make_log(hours=Decimal("2"), is_billable=False)
        self.make_log(hours=Decimal("4"), is_billable=True, logged_by=self.manager)

        summary = WorkLogEntry.user_month_summary(
            self.dev, self.today.year, self.today.month)

        self.assertEqual(summary["total"], Decimal("8"))
        self.assertEqual(summary["billable"], Decimal("6"))
        self.assertEqual(summary["non_billable"], Decimal("2"))
        self.assertEqual(summary["entries"], 2)
        self.assertEqual(summary["billable_percent"], 75)

    def test_user_month_summary_excludes_other_months(self):
        self.make_log(hours=Decimal("5"))
        self.make_log(hours=Decimal("3"), date=date(2020, 1, 15))

        summary = WorkLogEntry.user_month_summary(
            self.dev, self.today.year, self.today.month)

        self.assertEqual(summary["total"], Decimal("5"))

    def test_project_summary_totals_and_approved(self):
        self.make_log(hours=Decimal("5"), is_billable=True,
                      approval_status=WorkLogEntry.Approval.APPROVED)
        self.make_log(hours=Decimal("3"), is_billable=False)
        other = Project.objects.create(client=self.client_obj, name="Other")
        self.make_log(project=other, hours=Decimal("9"))

        summary = WorkLogEntry.project_summary(self.project)

        self.assertEqual(summary["total"], Decimal("8"))
        self.assertEqual(summary["billable"], Decimal("5"))
        self.assertEqual(summary["non_billable"], Decimal("3"))
        self.assertEqual(summary["approved"], Decimal("5"))

    def test_summaries_are_zero_not_none_when_empty(self):
        summary = WorkLogEntry.project_summary(self.project)
        self.assertEqual(summary["total"], Decimal("0"))
        self.assertEqual(summary["billable_percent"], 0)


class TimesheetPageTests(WorkLogBase):
    def test_timesheet_groups_by_week_with_totals_and_split(self):
        monday = self.today - timedelta(days=self.today.weekday())
        self.make_log(date=monday, hours=Decimal("6"), is_billable=True,
                      description="Billable work")
        self.make_log(date=monday, hours=Decimal("2"), is_billable=False,
                      description="Internal meeting")
        self.client.login(username="dev", password="x")

        r = self.client.get(reverse("worklogs:mine"))

        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Billable work")
        self.assertContains(r, "Internal meeting")
        week = r.context["weeks"][0]
        self.assertEqual(week["total"], Decimal("8"))
        self.assertEqual(week["billable"], Decimal("6"))
        self.assertEqual(week["non_billable"], Decimal("2"))
        self.assertEqual(week["billable_percent"], 75)
        self.assertContains(r, "Submit week for approval")

    def test_timesheet_shows_only_own_entries(self):
        self.make_log(hours=Decimal("3"), description="Mine")
        self.make_log(hours=Decimal("3"), description="Theirs",
                      logged_by=self.manager)
        self.client.login(username="dev", password="x")

        r = self.client.get(reverse("worklogs:mine"))

        self.assertContains(r, "Mine")
        self.assertNotContains(r, "Theirs")

    def test_timesheet_empty_state(self):
        self.client.login(username="dev", password="x")
        r = self.client.get(reverse("worklogs:mine"))
        self.assertContains(r, "No time logged")

    def test_logging_from_the_timesheet_picks_the_project_from_the_form(self):
        task = self.make_task()
        self.client.login(username="dev", password="x")

        self.client.post(reverse("worklogs:create"), {
            "project": self.project.pk, "task": task.pk,
            "date": self.today.isoformat(), "description": "From timesheet",
            "category": "DEV", "hours": "2", "is_billable": "1",
        })

        entry = WorkLogEntry.objects.get()
        self.assertEqual(entry.project, self.project)
        self.assertEqual(entry.task, task)

    def test_a_task_from_another_project_is_ignored(self):
        other = Project.objects.create(client=self.client_obj, name="Other")
        stray = Task.objects.create(project=other, title="Elsewhere")
        self.client.login(username="dev", password="x")

        self.client.post(reverse("worklogs:create"), {
            "project": self.project.pk, "task": stray.pk,
            "date": self.today.isoformat(), "description": "Mismatched",
            "category": "DEV", "hours": "2",
        })

        entry = WorkLogEntry.objects.get()
        self.assertIsNone(entry.task_id)

    def test_approvals_queue_lists_pending_for_a_manager(self):
        self.make_log(hours=Decimal("4"), description="Needs sign-off",
                      approval_status=WorkLogEntry.Approval.SUBMITTED)
        self.make_log(hours=Decimal("4"), description="Still a draft")
        self.client.login(username="mgr", password="x")

        r = self.client.get(reverse("worklogs:approvals"))

        self.assertContains(r, "Needs sign-off")
        self.assertNotContains(r, "Still a draft")
        self.assertEqual(r.context["pending_count"], 1)

    def test_approvals_queue_is_scoped_to_direct_reports(self):
        stranger = User.objects.create_user("stranger", password="x", role="DEV")
        self.make_log(hours=Decimal("4"), description="Not yours",
                      logged_by=stranger,
                      approval_status=WorkLogEntry.Approval.SUBMITTED)
        self.client.login(username="mgr", password="x")

        r = self.client.get(reverse("worklogs:approvals"))

        self.assertNotContains(r, "Not yours")

    def test_approver_with_the_perm_sees_everyones(self):
        stranger = User.objects.create_user("stranger", password="x", role="DEV")
        self.make_log(hours=Decimal("4"), description="Anyone's log",
                      logged_by=stranger,
                      approval_status=WorkLogEntry.Approval.SUBMITTED)
        self.client.login(username="boss", password="x")  # Super Admin

        r = self.client.get(reverse("worklogs:approvals"))

        self.assertContains(r, "Anyone&#x27;s log")

    def test_non_approver_is_redirected_away_from_the_queue(self):
        loner = User.objects.create_user("loner", password="x", role="DEV")
        self.client.force_login(loner)
        r = self.client.get(reverse("worklogs:approvals"))
        self.assertRedirects(r, reverse("worklogs:mine"))


class ProjectWorkLogSectionTests(WorkLogBase):
    def test_filters_narrow_the_list(self):
        self.make_log(hours=Decimal("3"), description="Dana billable",
                      is_billable=True)
        self.make_log(hours=Decimal("2"), description="Dana internal",
                      is_billable=False)
        self.make_log(hours=Decimal("4"), description="Mia work",
                      logged_by=self.manager)
        self.client.login(username="dev", password="x")
        url = self.project.get_absolute_url()

        by_member = self.client.get(url, {"member": self.dev.pk})
        self.assertContains(by_member, "Dana billable")
        self.assertNotContains(by_member, "Mia work")

        billable_only = self.client.get(url, {"billable": "yes"})
        self.assertContains(billable_only, "Dana billable")
        self.assertNotContains(billable_only, "Dana internal")

    def test_date_range_filter(self):
        self.make_log(hours=Decimal("3"), description="Recent")
        self.make_log(hours=Decimal("3"), description="Ancient",
                      date=date(2020, 1, 15))
        self.client.login(username="dev", password="x")

        r = self.client.get(self.project.get_absolute_url(),
                            {"from": date(2020, 1, 1).isoformat(),
                             "to": date(2020, 12, 31).isoformat()})

        self.assertContains(r, "Ancient")
        self.assertNotContains(r, "Recent")

    def test_approval_status_filter(self):
        self.make_log(hours=Decimal("3"), description="Signed off",
                      approval_status=WorkLogEntry.Approval.APPROVED)
        self.make_log(hours=Decimal("3"), description="Draft entry")
        self.client.login(username="dev", password="x")

        r = self.client.get(self.project.get_absolute_url(),
                            {"approval": "APPROVED"})

        self.assertContains(r, "Signed off")
        self.assertNotContains(r, "Draft entry")

    def test_section_shows_task_link_and_billable_totals(self):
        task = self.make_task(title="Homepage build")
        self.make_log(task=task, hours=Decimal("5"), is_billable=True)
        self.make_log(hours=Decimal("2"), is_billable=False)
        self.client.login(username="dev", password="x")

        r = self.client.get(self.project.get_absolute_url())

        self.assertContains(r, "Homepage build")
        self.assertContains(r, "7 h total")
        self.assertContains(r, "5 h billable")
        self.assertContains(r, "2 h non-billable")
