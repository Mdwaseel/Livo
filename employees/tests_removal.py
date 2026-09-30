"""Offboarding and permanent deletion of an employee.

The guards are the point of these tests. Everything here is either irreversible
or locks somebody out, so "the button was hidden" is not a control — each view
is exercised directly, the way a stale tab or a hand-written POST would reach it.
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import Role
from employees.models import EmployeeProfile, SalaryRecord

User = get_user_model()


class OffboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # OWNER -> Super Admin, PM -> Project Manager (no employees.delete),
        # DEV -> Developer. See accounts' legacy-role signal.
        cls.boss = User.objects.create_user("boss", password="x", role="OWNER")
        cls.other_boss = User.objects.create_user("boss2", password="x", role="OWNER")
        cls.pm = User.objects.create_user("pm", password="x", role="PM")
        cls.dev = User.objects.create_user(
            "dev", password="x", role="DEV", first_name="Dev", last_name="Eloper")
        cls.profile = cls.dev.employee_profile

    def url(self, name, profile=None):
        return reverse(f"employees:{name}", args=[(profile or self.profile).pk])

    # ---------- offboard ----------

    def test_offboard_revokes_login_and_keeps_the_record(self):
        SalaryRecord.objects.create(employee=self.profile, month=date(2026, 9, 1),
                                    gross=1000, net_payable=1000)
        self.client.login(username="boss", password="x")
        r = self.client.post(self.url("offboard"),
                             {"status": "RESIGNED", "date_of_exit": "2026-09-30"})
        self.assertEqual(r.status_code, 302)

        self.dev.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertFalse(self.dev.is_active)
        self.assertEqual(self.profile.status, "RESIGNED")
        self.assertEqual(self.profile.date_of_exit, date(2026, 9, 30))
        # The record survives — that is the whole difference from a delete.
        self.assertTrue(EmployeeProfile.objects.filter(pk=self.profile.pk).exists())
        self.assertEqual(self.profile.salary_records.count(), 1)

    def test_offboarded_user_cannot_sign_in(self):
        self.client.login(username="boss", password="x")
        self.client.post(self.url("offboard"), {"status": "RESIGNED"})
        self.client.logout()
        self.assertFalse(self.client.login(username="dev", password="x"))

    def test_offboard_defaults_to_today_and_resigned(self):
        self.client.login(username="boss", password="x")
        self.client.post(self.url("offboard"), {})
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, "RESIGNED")
        self.assertIsNotNone(self.profile.date_of_exit)

    def test_junk_status_does_not_become_a_status(self):
        self.client.login(username="boss", password="x")
        self.client.post(self.url("offboard"), {"status": "ACTIVE"})
        self.profile.refresh_from_db()
        # ACTIVE is a real choice but not an *exit* one — an offboard that left
        # somebody ACTIVE would read as a no-op in the directory.
        self.assertEqual(self.profile.status, "RESIGNED")

    def test_offboard_needs_employees_delete(self):
        self.client.login(username="pm", password="x")
        self.client.post(self.url("offboard"), {"status": "RESIGNED"})
        self.dev.refresh_from_db()
        self.assertTrue(self.dev.is_active)

    def test_cannot_offboard_yourself(self):
        self.client.login(username="boss", password="x")
        self.client.post(self.url("offboard", self.boss.employee_profile),
                         {"status": "RESIGNED"})
        self.boss.refresh_from_db()
        self.assertTrue(self.boss.is_active)

    def test_get_does_not_offboard(self):
        self.client.login(username="boss", password="x")
        r = self.client.get(self.url("offboard"))
        self.assertEqual(r.status_code, 405)
        self.dev.refresh_from_db()
        self.assertTrue(self.dev.is_active)

    def test_reactivate_restores_access(self):
        self.client.login(username="boss", password="x")
        self.client.post(self.url("offboard"), {"status": "RESIGNED"})
        self.client.post(self.url("reactivate"), {})
        self.dev.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertTrue(self.dev.is_active)
        self.assertEqual(self.profile.status, "ACTIVE")
        self.assertIsNone(self.profile.date_of_exit)
        self.client.logout()
        self.assertTrue(self.client.login(username="dev", password="x"))

    # ---------- the last super admin ----------

    def test_a_super_admin_can_be_offboarded_while_another_remains(self):
        """The guard is "the last one", not "any of them" — otherwise a super
        admin who leaves the company could never be removed."""
        self.client.login(username="boss", password="x")
        self.client.post(self.url("offboard", self.other_boss.employee_profile), {})
        self.other_boss.refresh_from_db()
        self.assertFalse(self.other_boss.is_active)

    def test_the_last_super_admin_is_protected(self):
        solo = User.objects.create_superuser("solo", password="x")
        # Everyone else who could grant the role back is already gone.
        for u in (self.boss, self.other_boss):
            u.is_active = False
            u.save(update_fields=["is_active"])
        # A DIFFERENT actor with the right to do it, so what stops this is the
        # "nobody left to grant it back" rule and not the self-removal one.
        actor = User.objects.create_user("mgr", password="x")
        actor.primary_role = Role.objects.get(name="Manager")
        actor.save()
        self.client.login(username="mgr", password="x")
        self.client.post(self.url("offboard", solo.employee_profile), {})
        solo.refresh_from_db()
        self.assertTrue(solo.is_active)


class DeleteEmployeeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.boss = User.objects.create_user("boss", password="x", role="OWNER")
        cls.spare_boss = User.objects.create_user("boss2", password="x", role="OWNER")
        cls.manager = User.objects.create_user("mgr", password="x")
        cls.manager.primary_role = Role.objects.get(name="Manager")
        cls.manager.save()
        cls.dev = User.objects.create_user(
            "dev", password="x", role="DEV", first_name="Dev", last_name="Eloper")
        cls.profile = cls.dev.employee_profile

    def url(self, profile=None):
        return reverse("employees:delete", args=[(profile or self.profile).pk])

    def test_delete_removes_the_person_and_their_payroll(self):
        SalaryRecord.objects.create(employee=self.profile, month=date(2026, 9, 1),
                                    gross=1000, net_payable=1000)
        self.client.login(username="boss", password="x")
        r = self.client.post(self.url(), {"confirm": "dev"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(User.objects.filter(pk=self.dev.pk).exists())
        self.assertFalse(EmployeeProfile.objects.filter(pk=self.profile.pk).exists())
        self.assertEqual(SalaryRecord.objects.count(), 0)

    def test_wrong_confirmation_deletes_nothing(self):
        self.client.login(username="boss", password="x")
        self.client.post(self.url(), {"confirm": "Dev Eloper"})
        self.assertTrue(User.objects.filter(pk=self.dev.pk).exists())

    def test_missing_confirmation_deletes_nothing(self):
        self.client.login(username="boss", password="x")
        self.client.post(self.url(), {})
        self.assertTrue(User.objects.filter(pk=self.dev.pk).exists())

    def test_confirmation_is_case_insensitive(self):
        self.client.login(username="boss", password="x")
        self.client.post(self.url(), {"confirm": "  DEV  "})
        self.assertFalse(User.objects.filter(pk=self.dev.pk).exists())

    def test_manager_holds_employees_delete_but_cannot_erase(self):
        """The permission matrix grants Manager `employees.delete` — that buys
        offboarding, not the destruction of payroll history."""
        self.client.login(username="mgr", password="x")
        self.client.post(self.url(), {"confirm": "dev"})
        self.assertTrue(User.objects.filter(pk=self.dev.pk).exists())

    def test_cannot_delete_yourself(self):
        self.client.login(username="boss", password="x")
        self.client.post(self.url(self.boss.employee_profile), {"confirm": "boss"})
        self.assertTrue(User.objects.filter(pk=self.boss.pk).exists())

    def test_get_does_not_delete(self):
        self.client.login(username="boss", password="x")
        r = self.client.get(self.url())
        self.assertEqual(r.status_code, 405)
        self.assertTrue(User.objects.filter(pk=self.dev.pk).exists())

    def test_anonymous_is_redirected_to_login(self):
        r = self.client.post(self.url(), {"confirm": "dev"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])
        self.assertTrue(User.objects.filter(pk=self.dev.pk).exists())


class RemovalUITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.boss = User.objects.create_user("boss", password="x", role="OWNER")
        cls.spare_boss = User.objects.create_user("boss2", password="x", role="OWNER")
        cls.pm = User.objects.create_user("pm", password="x", role="PM")
        cls.dev = User.objects.create_user("dev", password="x", role="DEV")
        cls.profile = cls.dev.employee_profile

    def test_super_admin_sees_both_actions(self):
        self.client.login(username="boss", password="x")
        r = self.client.get(self.profile.get_absolute_url())
        self.assertContains(r, "Offboard")
        self.assertContains(r, "Delete permanently")

    def test_viewer_without_delete_sees_neither(self):
        self.client.login(username="pm", password="x")
        r = self.client.get(self.profile.get_absolute_url())
        self.assertNotContains(r, "Delete permanently")
        self.assertNotContains(r, "danger-zone")

    def test_your_own_profile_offers_no_way_to_remove_you(self):
        self.client.login(username="boss", password="x")
        r = self.client.get(self.boss.employee_profile.get_absolute_url())
        self.assertNotContains(r, "Delete permanently")
        self.assertContains(r, "You can&#x27;t remove your own account")

    def test_offboarded_profile_offers_restore_instead(self):
        self.dev.is_active = False
        self.dev.save(update_fields=["is_active"])
        self.client.login(username="boss", password="x")
        r = self.client.get(self.profile.get_absolute_url())
        self.assertContains(r, "Restore access")
        self.assertNotContains(r, "Offboard</button>")
