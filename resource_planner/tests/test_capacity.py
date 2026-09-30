"""Capacity arithmetic — pure functions, no database.

Every utilisation percentage and every colour in the module is this file's
output, so these are the tests that matter most.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.test import SimpleTestCase

from resource_planner import capacity as cap
from resource_planner.models import CapacityProfile

MONDAY = date(2026, 7, 6)
SUNDAY = MONDAY + timedelta(days=6)


class FakeLeave:
    """Stands in for a LeaveRecord without needing a database row."""

    Status = type("Status", (), {"REQUESTED": "REQUESTED", "APPROVED": "APPROVED"})

    def __init__(self, start, end, *, user_id=1, half=False,
                 status="APPROVED", kind="VACATION"):
        self.start_date, self.end_date = start, end
        self.user_id = user_id
        self.is_half_day = half
        self.status = status
        self.kind = kind

    @property
    def is_company_wide(self):
        return self.user_id is None

    @property
    def blocks_capacity(self):
        return self.status == "APPROVED"


def profile(daily=8, days="12345", user_id=1):
    instance = CapacityProfile(daily_hours=Decimal(str(daily)), working_days=days)
    instance.user_id = user_id
    return instance


class DateHelperTests(SimpleTestCase):
    def test_week_start_is_monday(self):
        self.assertEqual(cap.week_start(date(2026, 7, 9)), MONDAY)
        self.assertEqual(cap.week_start(MONDAY), MONDAY)
        self.assertEqual(cap.week_start(SUNDAY), MONDAY)

    def test_month_bounds(self):
        self.assertEqual(cap.month_start(date(2026, 7, 17)), date(2026, 7, 1))
        self.assertEqual(cap.month_end(date(2026, 7, 17)), date(2026, 7, 31))

    def test_month_end_handles_february(self):
        self.assertEqual(cap.month_end(date(2026, 2, 10)), date(2026, 2, 28))
        self.assertEqual(cap.month_end(date(2028, 2, 10)), date(2028, 2, 29))

    def test_iter_weeks_clips_to_the_range(self):
        weeks = cap.iter_weeks(date(2026, 7, 8), date(2026, 7, 20))
        self.assertEqual(weeks[0][0], date(2026, 7, 8))     # clipped, not Monday
        self.assertEqual(weeks[-1][1], date(2026, 7, 20))


class WorkingDayTests(SimpleTestCase):
    def test_default_week_is_monday_to_friday(self):
        person = profile()
        self.assertTrue(person.works_on(MONDAY))
        self.assertFalse(person.works_on(MONDAY + timedelta(days=5)))  # Saturday

    def test_custom_working_days(self):
        person = profile(days="67")  # weekend worker
        self.assertFalse(person.works_on(MONDAY))
        self.assertTrue(person.works_on(MONDAY + timedelta(days=5)))

    def test_working_days_between_counts_only_working_days(self):
        self.assertEqual(profile().working_days_between(MONDAY, SUNDAY), 5)

    def test_gross_capacity_is_daily_times_working_days(self):
        self.assertEqual(cap.gross_capacity_between(profile(), MONDAY, SUNDAY),
                         Decimal("40"))
        self.assertEqual(cap.gross_capacity_between(profile(6), MONDAY, SUNDAY),
                         Decimal("30"))


class LeaveCalendarTests(SimpleTestCase):
    def test_approved_leave_removes_the_whole_day(self):
        calendar = cap.LeaveCalendar([FakeLeave(MONDAY, MONDAY)])
        self.assertEqual(calendar.fraction_off(1, MONDAY), Decimal("1"))
        self.assertEqual(cap.daily_capacity(profile(), MONDAY, calendar), Decimal("0"))

    def test_half_day_removes_half(self):
        calendar = cap.LeaveCalendar([FakeLeave(MONDAY, MONDAY, half=True)])
        self.assertEqual(cap.daily_capacity(profile(), MONDAY, calendar),
                         Decimal("4.00"))

    def test_requested_leave_does_not_reduce_capacity(self):
        """Otherwise anyone could free their calendar by asking."""
        calendar = cap.LeaveCalendar(
            [FakeLeave(MONDAY, MONDAY, status="REQUESTED")])
        self.assertEqual(cap.daily_capacity(profile(), MONDAY, calendar),
                         Decimal("8"))

    def test_requested_leave_is_still_surfaced_as_pending(self):
        calendar = cap.LeaveCalendar(
            [FakeLeave(MONDAY, MONDAY, status="REQUESTED")])
        self.assertTrue(calendar.has_pending(1, MONDAY))

    def test_company_wide_leave_applies_to_everybody(self):
        calendar = cap.LeaveCalendar([FakeLeave(MONDAY, MONDAY, user_id=None)])
        for user_id in (1, 2, 99):
            self.assertEqual(calendar.fraction_off(user_id, MONDAY), Decimal("1"))

    def test_overlapping_leave_never_exceeds_one_day(self):
        """Two half-days on the same date is one day off, not one and a half —
        stacking would make capacity negative."""
        calendar = cap.LeaveCalendar([
            FakeLeave(MONDAY, MONDAY, half=True),
            FakeLeave(MONDAY, MONDAY, half=True),
        ])
        self.assertEqual(calendar.fraction_off(1, MONDAY), Decimal("0.5"))

    def test_full_day_wins_over_a_half_day_on_the_same_date(self):
        calendar = cap.LeaveCalendar([
            FakeLeave(MONDAY, MONDAY, half=True),
            FakeLeave(MONDAY, MONDAY),
        ])
        self.assertEqual(calendar.fraction_off(1, MONDAY), Decimal("1"))

    def test_company_holiday_and_personal_leave_do_not_stack(self):
        calendar = cap.LeaveCalendar([
            FakeLeave(MONDAY, MONDAY, user_id=None),
            FakeLeave(MONDAY, MONDAY),
        ])
        self.assertEqual(calendar.fraction_off(1, MONDAY), Decimal("1"))

    def test_leave_on_a_non_working_day_costs_nothing(self):
        """Saturday leave for a Mon-Fri worker must not create negative
        capacity."""
        saturday = MONDAY + timedelta(days=5)
        calendar = cap.LeaveCalendar([FakeLeave(saturday, saturday)])
        self.assertEqual(cap.daily_capacity(profile(), saturday, calendar),
                         Decimal("0"))
        self.assertEqual(
            cap.capacity_between(profile(), MONDAY, SUNDAY, calendar),
            Decimal("40"))

    def test_a_week_of_leave_zeroes_capacity(self):
        calendar = cap.LeaveCalendar([FakeLeave(MONDAY, SUNDAY)])
        self.assertEqual(
            cap.capacity_between(profile(), MONDAY, SUNDAY, calendar),
            Decimal("0"))

    def test_leave_hours_reports_what_was_lost(self):
        calendar = cap.LeaveCalendar([FakeLeave(MONDAY, MONDAY)])
        self.assertEqual(
            cap.leave_hours_between(profile(), MONDAY, SUNDAY, calendar),
            Decimal("8"))


class UtilizationTests(SimpleTestCase):
    def test_basic_percentage(self):
        self.assertEqual(cap.utilization_percent(20, 40), 50.0)

    def test_over_capacity_is_not_capped(self):
        """Overload is the finding; capping would hide it."""
        self.assertEqual(cap.utilization_percent(60, 40), 150.0)

    def test_zero_capacity_with_work_is_flagged_not_divided(self):
        self.assertEqual(cap.utilization_percent(5, 0), 200.0)

    def test_zero_capacity_with_no_work_is_zero(self):
        self.assertEqual(cap.utilization_percent(0, 0), 0.0)

    def test_none_inputs_are_tolerated(self):
        self.assertEqual(cap.utilization_percent(None, None), 0.0)


class StatusTests(SimpleTestCase):
    def test_green_below_75(self):
        self.assertEqual(cap.status_for(50, 40), cap.STATUS_AVAILABLE)
        self.assertEqual(cap.status_for(74.9, 40), cap.STATUS_AVAILABLE)

    def test_yellow_between_75_and_100(self):
        self.assertEqual(cap.status_for(75, 40), cap.STATUS_NEAR)
        self.assertEqual(cap.status_for(99.9, 40), cap.STATUS_NEAR)

    def test_red_at_and_above_100(self):
        self.assertEqual(cap.status_for(100, 40), cap.STATUS_OVER)
        self.assertEqual(cap.status_for(180, 40), cap.STATUS_OVER)

    def test_zero_capacity_is_its_own_state_not_green(self):
        """Someone on leave all week is not "available" — colouring them green
        sends a manager to hand work to somebody on a beach."""
        self.assertEqual(cap.status_for(0, 0), cap.STATUS_OFF)

    def test_every_status_has_a_label(self):
        for status in (cap.STATUS_AVAILABLE, cap.STATUS_NEAR,
                       cap.STATUS_OVER, cap.STATUS_OFF):
            self.assertIn(status, cap.STATUS_LABELS)
