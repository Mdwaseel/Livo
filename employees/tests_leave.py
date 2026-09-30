"""Leave and payroll.

The arithmetic here decides what lands in somebody's bank account, so most of
these tests are about the *maths* rather than the screens: what a leave day is,
when the allowance runs out, and what happens at a month boundary. A screen
that renders a wrong number is a bug; a payroll run that pays a wrong number is
a different kind of problem.

The recurring theme is that a deduction must not depend on anything the
employee didn't do — not on which month they took their leave in, not on
whether a weekend fell inside it, and not on when payroll happened to be run.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import Role
from core.models import AgencySettings, Workspace
from employees import leave as leave_math
from employees import payroll
from employees.models import EmployeeProfile, SalaryRecord
from resource_planner.models import LeaveRecord

User = get_user_model()


def approved(user, start, end, kind=LeaveRecord.Kind.VACATION, **extra):
    return LeaveRecord.objects.create(
        user=user, kind=kind, start_date=start, end_date=end,
        status=LeaveRecord.Status.APPROVED,
        workspace_id=getattr(user, "workspace_id", None), **extra)


class LeaveFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        settings_row = AgencySettings.load()
        settings_row.default_annual_leave_days = 12
        settings_row.save()

        cls.hr = User.objects.create_user(
            "hr", password="x", primary_role=Role.objects.get(name="HR"))
        cls.manager = User.objects.create_user(
            "boss", password="x", primary_role=Role.objects.get(name="Manager"))
        cls.anna = User.objects.create_user(
            "anna", password="x", first_name="Anna",
            primary_role=Role.objects.get(name="Developer"),
            reports_to=cls.manager)
        cls.profile, _ = EmployeeProfile.objects.get_or_create(user=cls.anna)
        cls.profile.salary = Decimal("60000")     # ₹2,000 a day at ÷30
        cls.profile.save()


# ---------------------------------------------------------------------------
# what a leave day is
# ---------------------------------------------------------------------------

class LeaveDayCountingTests(LeaveFixture):

    def test_a_weekend_inside_the_range_is_not_leave(self):
        """Fri to Mon is two working days off, not four. Counting calendar days
        would charge people for the weekend they were never working anyway."""
        record = approved(self.anna, date(2026, 3, 6), date(2026, 3, 9))  # Fri–Mon
        self.assertEqual(leave_math.countable_days(record), Decimal("2"))

    def test_a_public_holiday_inside_the_range_is_not_leave(self):
        """A holiday is not somebody's leave to spend, even when it lands in
        the middle of a week they took off."""
        LeaveRecord.objects.create(
            user=None, kind=LeaveRecord.Kind.HOLIDAY,
            status=LeaveRecord.Status.APPROVED,
            start_date=date(2026, 3, 11), end_date=date(2026, 3, 11))
        record = approved(self.anna, date(2026, 3, 9), date(2026, 3, 13))
        self.assertEqual(leave_math.countable_days(record), Decimal("4"))

    def test_a_half_day_is_half_a_day(self):
        record = approved(self.anna, date(2026, 3, 10), date(2026, 3, 10),
                          is_half_day=True)
        self.assertEqual(leave_math.countable_days(record), Decimal("0.5"))

    def test_a_pending_request_changes_nothing(self):
        """Otherwise anyone could spend their own allowance by asking."""
        LeaveRecord.objects.create(
            user=self.anna, kind=LeaveRecord.Kind.VACATION,
            start_date=date(2026, 3, 9), end_date=date(2026, 3, 13),
            status=LeaveRecord.Status.REQUESTED)
        self.assertEqual(leave_math.balance(self.profile, 2026)["used"], Decimal("0"))

    def test_holidays_and_comp_off_never_touch_the_allowance(self):
        approved(self.anna, date(2026, 3, 9), date(2026, 3, 13),
                 kind=LeaveRecord.Kind.COMP_OFF)
        self.assertEqual(leave_math.balance(self.profile, 2026)["used"], Decimal("0"))


# ---------------------------------------------------------------------------
# balances
# ---------------------------------------------------------------------------

class BalanceTests(LeaveFixture):

    def test_allowance_falls_back_to_the_agency_default(self):
        self.assertEqual(leave_math.allowance_for(self.profile), 12)

    def test_a_personal_override_wins(self):
        self.profile.annual_leave_days = 20
        self.profile.save()
        self.assertEqual(leave_math.allowance_for(self.profile), 20)

    def test_remaining_never_goes_negative_and_over_takes_up_the_slack(self):
        """Two figures rather than one signed number: "0 left" and "3 over" are
        both true and both need saying, and a negative "days remaining" reads
        as a rendering fault."""
        approved(self.anna, date(2026, 2, 2), date(2026, 2, 20))  # 15 working days
        balance = leave_math.balance(self.profile, 2026)
        self.assertEqual(balance["used"], Decimal("15"))
        self.assertEqual(balance["remaining"], Decimal("0"))
        self.assertEqual(balance["over"], Decimal("3"))

    def test_unpaid_leave_is_tracked_apart_from_the_pool(self):
        """It never consumes allowance and always costs pay — that is what the
        word means. Someone with days left who files unpaid leave still has
        them afterwards."""
        approved(self.anna, date(2026, 3, 9), date(2026, 3, 11),
                 kind=LeaveRecord.Kind.UNPAID)
        balance = leave_math.balance(self.profile, 2026)
        self.assertEqual(balance["used"], Decimal("0"))
        self.assertEqual(balance["remaining"], Decimal("12"))
        self.assertEqual(balance["unpaid"], Decimal("3"))

    def test_the_allowance_resets_with_the_calendar_year(self):
        approved(self.anna, date(2025, 3, 3), date(2025, 3, 21))
        self.assertEqual(leave_math.balance(self.profile, 2026)["used"],
                         Decimal("0"))


# ---------------------------------------------------------------------------
# deductions
# ---------------------------------------------------------------------------

class DeductionTests(LeaveFixture):

    def test_leave_within_the_allowance_costs_nothing(self):
        approved(self.anna, date(2026, 3, 9), date(2026, 3, 13))  # 5 days of 12
        figures = leave_math.deduction_for_month(self.profile, date(2026, 3, 1))
        self.assertEqual(figures["excess_days"], Decimal("0"))
        self.assertEqual(figures["deduction"], Decimal("0.00"))

    def test_only_the_days_beyond_the_allowance_are_charged(self):
        approved(self.anna, date(2026, 3, 2), date(2026, 3, 20))  # 15 working days
        figures = leave_math.deduction_for_month(self.profile, date(2026, 3, 1))
        self.assertEqual(figures["excess_days"], Decimal("3"))
        # ₹60,000 ÷ 30 = ₹2,000/day × 3
        self.assertEqual(figures["deduction"], Decimal("6000.00"))

    def test_the_charge_does_not_depend_on_which_month_the_leave_fell_in(self):
        """The whole point of running the allowance year-to-date. Ten days in
        February and five in March must cost the same as five then ten — the
        employee took fifteen days either way."""
        approved(self.anna, date(2026, 2, 2), date(2026, 2, 13))   # 10 days
        approved(self.anna, date(2026, 3, 2), date(2026, 3, 6))    # 5 days
        february = leave_math.deduction_for_month(self.profile, date(2026, 2, 1))
        march = leave_math.deduction_for_month(self.profile, date(2026, 3, 1))
        self.assertEqual(february["excess_days"], Decimal("0"))    # still inside 12
        self.assertEqual(march["excess_days"], Decimal("3"))       # 15 − 12
        self.assertEqual(february["deduction"] + march["deduction"],
                         Decimal("6000.00"))

    def test_unpaid_leave_is_charged_from_the_first_day(self):
        approved(self.anna, date(2026, 3, 9), date(2026, 3, 11),
                 kind=LeaveRecord.Kind.UNPAID)
        figures = leave_math.deduction_for_month(self.profile, date(2026, 3, 1))
        self.assertEqual(figures["unpaid_days"], Decimal("3"))
        self.assertEqual(figures["deduction"], Decimal("6000.00"))

    def test_leave_spanning_a_month_boundary_is_split_between_them(self):
        """A record from 27 Feb to 4 Mar must not be charged twice, nor land
        wholly in whichever month happens to be generated first."""
        approved(self.anna, date(2026, 2, 25), date(2026, 3, 6),
                 kind=LeaveRecord.Kind.UNPAID)
        february = leave_math.deduction_for_month(self.profile, date(2026, 2, 1))
        march = leave_math.deduction_for_month(self.profile, date(2026, 3, 1))
        # Wed 25 – Fri 27 February, then Mon 2 – Fri 6 March. The weekend
        # between belongs to neither month because it is not leave at all.
        self.assertEqual(february["unpaid_days"], Decimal("3"))
        self.assertEqual(march["unpaid_days"], Decimal("5"))
        # The halves add up to the whole: charged once, in the right months.
        self.assertEqual(february["unpaid_days"] + march["unpaid_days"],
                         leave_math.countable_days(
                             LeaveRecord.objects.get(kind=LeaveRecord.Kind.UNPAID)))


# ---------------------------------------------------------------------------
# payroll
# ---------------------------------------------------------------------------

class PayrollTests(LeaveFixture):

    def test_generating_a_month_builds_a_row_with_the_deduction_applied(self):
        approved(self.anna, date(2026, 3, 2), date(2026, 3, 20))  # 3 days over
        payroll.generate_month(date(2026, 3, 1), actor=self.hr)
        record = SalaryRecord.objects.get(employee=self.profile,
                                          month=date(2026, 3, 1))
        self.assertEqual(record.gross, Decimal("60000.00"))
        self.assertEqual(record.leave_deduction, Decimal("6000.00"))
        self.assertEqual(record.net_payable, Decimal("54000.00"))

    def test_generating_twice_does_not_double_anything(self):
        approved(self.anna, date(2026, 3, 2), date(2026, 3, 20))
        payroll.generate_month(date(2026, 3, 1), actor=self.hr)
        payroll.generate_month(date(2026, 3, 1), actor=self.hr)
        rows = SalaryRecord.objects.filter(employee=self.profile,
                                           month=date(2026, 3, 1))
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().leave_deduction, Decimal("6000.00"))

    def test_a_paid_month_is_never_recalculated(self):
        """Once money has left the bank the record of it is history. Leave
        approved late must not rewrite a payslip somebody has already been
        paid against."""
        payroll.generate_month(date(2026, 3, 1), actor=self.hr)
        record = SalaryRecord.objects.get(employee=self.profile,
                                          month=date(2026, 3, 1))
        payroll.mark_paid(record, paid_on=date(2026, 3, 31),
                          mode=SalaryRecord.Mode.BANK, reference="UTR123",
                          actor=self.hr)

        approved(self.anna, date(2026, 3, 2), date(2026, 3, 20))
        payroll.generate_month(date(2026, 3, 1), actor=self.hr)

        record.refresh_from_db()
        self.assertEqual(record.net_payable, Decimal("60000.00"))
        self.assertEqual(record.reference, "UTR123")

    def test_a_manual_adjustment_survives_a_regenerate(self):
        """Otherwise nobody would trust the recalculate button."""
        payroll.generate_month(date(2026, 3, 1), actor=self.hr)
        record = SalaryRecord.objects.get(employee=self.profile,
                                          month=date(2026, 3, 1))
        record.adjustment = Decimal("5000")
        record.adjustment_note = "Diwali bonus"
        record.recalculate()
        record.save()

        payroll.generate_month(date(2026, 3, 1), actor=self.hr)
        record.refresh_from_db()
        self.assertEqual(record.adjustment, Decimal("5000"))
        self.assertEqual(record.net_payable, Decimal("65000.00"))

    def test_net_pay_is_never_negative(self):
        payroll.generate_month(date(2026, 3, 1), actor=self.hr)
        record = SalaryRecord.objects.get(employee=self.profile,
                                          month=date(2026, 3, 1))
        record.adjustment = Decimal("-90000")     # a recovery larger than pay
        self.assertEqual(record.recalculate(), Decimal("0"))

    def test_someone_who_left_before_the_month_gets_no_payslip(self):
        self.profile.date_of_exit = date(2026, 1, 31)
        self.profile.status = EmployeeProfile.Status.RESIGNED
        self.profile.save()
        payroll.generate_month(date(2026, 3, 1), actor=self.hr)
        self.assertFalse(SalaryRecord.objects.filter(
            employee=self.profile, month=date(2026, 3, 1)).exists())

    def test_someone_who_joins_mid_month_is_included(self):
        self.profile.date_of_joining = date(2026, 3, 20)
        self.profile.save()
        payroll.generate_month(date(2026, 3, 1), actor=self.hr)
        self.assertTrue(SalaryRecord.objects.filter(
            employee=self.profile, month=date(2026, 3, 1)).exists())


# ---------------------------------------------------------------------------
# the screens
# ---------------------------------------------------------------------------

class LeaveScreenTests(LeaveFixture):

    def test_anyone_can_open_their_own_leave_page(self):
        """No RBAC action gates it — asking for leave is not a privilege, and a
        Developer holds no `leaves` permission at all."""
        self.client.force_login(self.anna)
        response = self.client.get(reverse("employees:my_leave"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["balance"]["allowed"], 12)

    def test_requesting_leave_lands_as_pending_and_notifies_the_manager(self):
        self.client.force_login(self.anna)
        self.client.post(reverse("employees:leave_request"), {
            "kind": "VACATION", "start_date": "2026-03-09",
            "end_date": "2026-03-11", "note": "Family wedding"})
        record = LeaveRecord.objects.get(user=self.anna)
        self.assertEqual(record.status, LeaveRecord.Status.REQUESTED)
        self.assertEqual(record.note, "Family wedding")
        self.assertTrue(self.manager.notifications.exists())

    def test_a_reason_is_required(self):
        self.client.force_login(self.anna)
        self.client.post(reverse("employees:leave_request"), {
            "kind": "VACATION", "start_date": "2026-03-09",
            "end_date": "2026-03-11", "note": "  "})
        self.assertFalse(LeaveRecord.objects.filter(user=self.anna).exists())

    def test_past_dates_are_allowed_so_leave_can_be_logged_after_the_fact(self):
        self.client.force_login(self.anna)
        self.client.post(reverse("employees:leave_request"), {
            "kind": "SICK", "start_date": "2020-03-09",
            "end_date": "2020-03-09", "note": "Fever"})
        record = LeaveRecord.objects.get(user=self.anna)
        self.assertTrue(record.is_retrospective)
        # Still pending: a retrospective entry is a manager confirming it
        # happened, and it feeds a salary deduction.
        self.assertEqual(record.status, LeaveRecord.Status.REQUESTED)

    def test_overlapping_leave_is_refused(self):
        approved(self.anna, date(2026, 3, 9), date(2026, 3, 13))
        self.client.force_login(self.anna)
        self.client.post(reverse("employees:leave_request"), {
            "kind": "VACATION", "start_date": "2026-03-11",
            "end_date": "2026-03-12", "note": "Clash"})
        self.assertEqual(LeaveRecord.objects.filter(user=self.anna).count(), 1)

    def test_a_manager_can_approve_their_report(self):
        self.client.force_login(self.anna)
        self.client.post(reverse("employees:leave_request"), {
            "kind": "VACATION", "start_date": "2026-03-09",
            "end_date": "2026-03-11", "note": "Wedding"})
        record = LeaveRecord.objects.get(user=self.anna)

        self.client.force_login(self.manager)
        self.client.post(reverse("employees:leave_decide", args=[record.pk]),
                         {"action": "approve"})
        record.refresh_from_db()
        self.assertEqual(record.status, LeaveRecord.Status.APPROVED)
        self.assertEqual(record.approved_by, self.manager)
        self.assertIsNotNone(record.decided_at)
        self.assertTrue(self.anna.notifications.exists())

    def test_nobody_approves_their_own_leave(self):
        """The manager here holds `leaves.approve` for everyone — including,
        without this rule, themselves."""
        LeaveRecord.objects.create(
            user=self.manager, kind=LeaveRecord.Kind.VACATION,
            start_date=date(2026, 3, 9), end_date=date(2026, 3, 11),
            status=LeaveRecord.Status.REQUESTED)
        record = LeaveRecord.objects.get(user=self.manager)
        self.client.force_login(self.manager)
        self.client.post(reverse("employees:leave_decide", args=[record.pk]),
                         {"action": "approve"})
        record.refresh_from_db()
        self.assertEqual(record.status, LeaveRecord.Status.REQUESTED)

    def test_the_top_of_the_tree_logs_their_own_leave_straight_away(self):
        """The owner reports to nobody and may not approve their own request,
        so without this their leave would sit pending for ever — never counted,
        never deducted, never in anyone's queue."""
        owner = User.objects.create_user(
            "owner", password="x",
            primary_role=Role.objects.get(name="Super Admin"))
        self.client.force_login(owner)
        self.client.post(reverse("employees:leave_request"), {
            "kind": "VACATION", "start_date": "2026-03-09",
            "end_date": "2026-03-11", "note": "Break"})
        record = LeaveRecord.objects.get(user=owner)
        self.assertEqual(record.status, LeaveRecord.Status.APPROVED)
        self.assertEqual(record.approved_by, owner)

    def test_a_manager_who_has_their_own_manager_still_asks(self):
        """The exception is "nobody above you", not "senior enough". A manager
        who reports to the owner goes through the owner like anyone else."""
        self.manager.reports_to = User.objects.create_user(
            "owner2", password="x",
            primary_role=Role.objects.get(name="Super Admin"))
        self.manager.save()
        self.client.force_login(self.manager)
        self.client.post(reverse("employees:leave_request"), {
            "kind": "VACATION", "start_date": "2026-03-09",
            "end_date": "2026-03-11", "note": "Break"})
        record = LeaveRecord.objects.get(user=self.manager)
        self.assertEqual(record.status, LeaveRecord.Status.REQUESTED)

    def test_a_developer_cannot_reach_the_approvals_queue(self):
        self.client.force_login(self.anna)
        response = self.client.get(reverse("employees:leave_approvals"))
        self.assertEqual(response.status_code, 302)

    def test_rejection_records_the_reason_for_the_employee(self):
        LeaveRecord.objects.create(
            user=self.anna, kind=LeaveRecord.Kind.VACATION,
            start_date=date(2026, 3, 9), end_date=date(2026, 3, 11),
            status=LeaveRecord.Status.REQUESTED)
        record = LeaveRecord.objects.get(user=self.anna)
        self.client.force_login(self.manager)
        self.client.post(reverse("employees:leave_decide", args=[record.pk]),
                         {"action": "reject", "decision_note": "Launch week"})
        record.refresh_from_db()
        self.assertEqual(record.status, LeaveRecord.Status.REJECTED)
        self.assertEqual(record.decision_note, "Launch week")
        # The employee's own words are not overwritten by the refusal.
        self.assertNotEqual(record.note, "Launch week")


class SalaryScreenTests(LeaveFixture):

    def test_everyone_can_see_their_own_pay_history(self):
        payroll.generate_month(date(2026, 3, 1), actor=self.hr)
        self.client.force_login(self.anna)
        response = self.client.get(reverse("employees:my_salary"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["records"]), 1)

    def test_a_developer_cannot_open_payroll(self):
        self.client.force_login(self.anna)
        response = self.client.get(reverse("employees:payroll"))
        self.assertEqual(response.status_code, 302)

    def test_hr_can_run_payroll_and_record_how_it_was_paid(self):
        self.client.force_login(self.hr)
        self.client.post(reverse("employees:payroll_generate"),
                         {"month": "2026-03"})
        record = SalaryRecord.objects.get(employee=self.profile,
                                          month=date(2026, 3, 1))
        self.client.post(reverse("employees:salary_pay", args=[record.pk]), {
            "paid_on": "2026-03-31", "mode": "UPI", "reference": "UPI-9981"})
        record.refresh_from_db()
        self.assertTrue(record.is_paid)
        self.assertEqual(record.mode, SalaryRecord.Mode.UPI)
        self.assertEqual(record.reference, "UPI-9981")
        self.assertEqual(record.paid_on, date(2026, 3, 31))
        self.assertTrue(self.anna.notifications.exists())

    def test_marking_paid_needs_a_payment_mode(self):
        self.client.force_login(self.hr)
        self.client.post(reverse("employees:payroll_generate"),
                         {"month": "2026-03"})
        record = SalaryRecord.objects.get(employee=self.profile,
                                          month=date(2026, 3, 1))
        self.client.post(reverse("employees:salary_pay", args=[record.pk]),
                         {"paid_on": "2026-03-31", "mode": ""})
        record.refresh_from_db()
        self.assertFalse(record.is_paid)


# ---------------------------------------------------------------------------
# partitioning
# ---------------------------------------------------------------------------

class LeaveWorkspaceTests(LeaveFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.partner = Workspace.objects.create(name="Northwind", slug="northwind")
        cls.partner_admin = User.objects.create_user(
            "pat", password="x", workspace=cls.partner,
            primary_role=Role.objects.get(name="Super Admin"))

    def test_a_partner_admin_sees_none_of_our_leave_requests(self):
        """They hold `leaves.approve` — super admins hold everything — so only
        the workspace filter stands between them and our team's leave."""
        LeaveRecord.objects.create(
            user=self.anna, kind=LeaveRecord.Kind.VACATION,
            start_date=date(2026, 3, 9), end_date=date(2026, 3, 11),
            status=LeaveRecord.Status.REQUESTED, workspace=Workspace.home())
        self.client.force_login(self.partner_admin)
        response = self.client.get(reverse("employees:leave_approvals"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["rows"], [])

    def test_a_partner_admin_sees_only_their_own_workspaces_payroll(self):
        """They do run payroll — for their own people. What they must never
        see is our salary bill, and `payroll.view` alone would show it to them
        because a super admin passes every RBAC check."""
        payroll.generate_month(date(2026, 3, 1), actor=self.hr)
        self.client.force_login(self.partner_admin)
        response = self.client.get(
            reverse("employees:payroll") + "?month=2026-03")
        self.assertEqual(response.status_code, 200)
        employees_shown = {r.employee.user.username
                           for r in response.context["records"]}
        self.assertNotIn("anna", employees_shown)
        self.assertNotIn("hr", employees_shown)
        self.assertEqual(employees_shown, {"pat"})

    def test_leave_is_stamped_with_the_requesters_workspace(self):
        self.client.force_login(self.partner_admin)
        self.client.post(reverse("employees:leave_request"), {
            "kind": "VACATION", "start_date": "2026-03-09",
            "end_date": "2026-03-11", "note": "Trip"})
        record = LeaveRecord.objects.get(user=self.partner_admin)
        self.assertEqual(record.workspace, self.partner)
