"""Model behaviour: derived capacity figures, defaults, and leave validation."""
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from resource_planner.models import (DEFAULT_DAILY_HOURS, CapacityProfile,
                                     LeaveRecord)

from .factories import MONDAY, Scenario, make_user


class CapacityProfileTests(TestCase):
    def test_defaults_are_eight_hours_monday_to_friday(self):
        user = make_user("newbie")
        profile = CapacityProfile.objects.create(user=user)
        self.assertEqual(profile.daily_hours, DEFAULT_DAILY_HOURS)
        self.assertEqual(profile.days_per_week, 5)
        self.assertEqual(profile.weekly_hours, Decimal("40"))

    def test_weekly_is_derived_from_daily_times_working_days(self):
        profile = CapacityProfile(daily_hours=Decimal("6"), working_days="1234")
        self.assertEqual(profile.weekly_hours, Decimal("24"))

    def test_monthly_is_derived_from_weekly(self):
        profile = CapacityProfile(daily_hours=Decimal("8"), working_days="12345")
        # 40 × 4.345 = 173.80
        self.assertEqual(profile.monthly_hours, Decimal("173.80"))

    def test_overrides_win_over_the_derived_figures(self):
        profile = CapacityProfile(daily_hours=Decimal("8"),
                                  weekly_hours_override=Decimal("32"),
                                  monthly_hours_override=Decimal("120"))
        self.assertEqual(profile.weekly_hours, Decimal("32"))
        self.assertEqual(profile.monthly_hours, Decimal("120"))

    def test_monthly_follows_a_weekly_override(self):
        """The derivation chains, so one override doesn't leave the other stale."""
        profile = CapacityProfile(daily_hours=Decimal("8"),
                                  weekly_hours_override=Decimal("20"))
        self.assertEqual(profile.monthly_hours, Decimal("86.90"))

    def test_default_for_returns_an_unsaved_profile(self):
        """An implicit 8 h/day is a default; writing it would make it look like
        a decision somebody made."""
        user = make_user("ghost")
        profile = CapacityProfile.default_for(user)
        self.assertIsNone(profile.pk)
        self.assertEqual(profile.daily_hours, DEFAULT_DAILY_HOURS)
        self.assertFalse(CapacityProfile.objects.filter(user=user).exists())

    def test_map_for_fills_gaps_with_defaults(self):
        saved = make_user("saved")
        missing = make_user("missing")
        CapacityProfile.objects.create(user=saved, daily_hours=Decimal("4"))
        mapping = CapacityProfile.map_for([saved, missing])
        self.assertEqual(mapping[saved.pk].daily_hours, Decimal("4"))
        self.assertEqual(mapping[missing.pk].daily_hours, DEFAULT_DAILY_HOURS)

    def test_map_for_is_a_single_query(self):
        users = [make_user(f"u{index}") for index in range(6)]
        with self.assertNumQueries(1):
            CapacityProfile.map_for(users)

    def test_working_days_must_be_iso_digits(self):
        profile = CapacityProfile(user=make_user("bad"), working_days="xyz")
        with self.assertRaises(ValidationError):
            profile.full_clean()

    def test_working_days_cannot_be_empty(self):
        profile = CapacityProfile(user=make_user("empty"), working_days="")
        with self.assertRaises(ValidationError):
            profile.full_clean()

    def test_working_days_cannot_repeat(self):
        profile = CapacityProfile(user=make_user("dupe"), working_days="112")
        with self.assertRaises(ValidationError):
            profile.full_clean()

    def test_one_profile_per_person(self):
        user = make_user("solo")
        CapacityProfile.objects.create(user=user)
        # atomic(): an IntegrityError leaves the surrounding transaction broken,
        # and without its own block every later query in this TestCase fails.
        with transaction.atomic(), self.assertRaises(IntegrityError):
            CapacityProfile.objects.create(user=user)


class LeaveRecordTests(TestCase):
    def test_end_before_start_is_rejected(self):
        record = LeaveRecord(user=make_user("x"), start_date=MONDAY,
                             end_date=MONDAY - timedelta(days=1))
        with self.assertRaises(ValidationError):
            record.full_clean(exclude=["user"])

    def test_day_count_is_inclusive(self):
        record = LeaveRecord(start_date=MONDAY, end_date=MONDAY)
        self.assertEqual(record.days, 1)
        self.assertEqual(
            LeaveRecord(start_date=MONDAY,
                        end_date=MONDAY + timedelta(days=4)).days, 5)

    def test_null_user_means_company_wide(self):
        record = LeaveRecord(start_date=MONDAY, end_date=MONDAY)
        self.assertTrue(record.is_company_wide)

    def test_only_approved_leave_blocks_capacity(self):
        for status, blocks in (
            (LeaveRecord.Status.APPROVED, True),
            (LeaveRecord.Status.REQUESTED, False),
            (LeaveRecord.Status.REJECTED, False),
            (LeaveRecord.Status.CANCELLED, False),
        ):
            with self.subTest(status=status):
                self.assertEqual(
                    LeaveRecord(status=status).blocks_capacity, blocks)

    def test_overlap_detection(self):
        record = LeaveRecord(start_date=date(2026, 7, 6), end_date=date(2026, 7, 10))
        self.assertTrue(record.overlaps(date(2026, 7, 8), date(2026, 7, 20)))
        self.assertTrue(record.overlaps(date(2026, 7, 1), date(2026, 7, 6)))
        self.assertFalse(record.overlaps(date(2026, 7, 11), date(2026, 7, 20)))
        self.assertFalse(record.overlaps(date(2026, 6, 1), date(2026, 7, 5)))


class NoDuplicationTests(TestCase):
    """The module's own rule: store only what nothing else can answer."""

    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_the_planner_owns_exactly_two_tables(self):
        from django.apps import apps
        models = {model.__name__ for model in
                  apps.get_app_config("resource_planner").get_models()}
        self.assertEqual(models, {"CapacityProfile", "LeaveRecord"})

    def test_no_hours_are_copied_out_of_the_task_or_work_log_tables(self):
        """Allocation is derived on read. If either model ever grows an
        `allocated_hours` column, this fails and somebody has to justify it."""
        for model in (CapacityProfile, LeaveRecord):
            fields = {field.name for field in model._meta.get_fields()}
            self.assertNotIn("allocated_hours", fields)
            self.assertNotIn("logged_hours", fields)
            self.assertNotIn("utilization", fields)
