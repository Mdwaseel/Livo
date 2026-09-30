from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import EmployeeDocument, EmployeeProfile, Skill

User = get_user_model()

PDF = b"%PDF-1.4 fake"


class EmployeeModuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # legacy CharField roles map to RBAC roles via the accounts signal:
        # PM -> Project Manager (employees.view, NO payroll.view)
        # OWNER -> Super Admin (everything)
        # DEV -> Developer (no employees perms at all)
        cls.hr_boss = User.objects.create_user("boss", password="x", role="OWNER")
        cls.pm = User.objects.create_user("pm", password="x", role="PM")
        cls.dev = User.objects.create_user(
            "dev", password="x", role="DEV", first_name="Dev", last_name="Eloper")
        cls.profile = cls.dev.employee_profile
        cls.profile.salary = 55000
        cls.profile.bank_account_number = "9876543210"
        cls.profile.address = "12 Baker Street"
        cls.profile.save()

    def test_signal_creates_profile(self):
        u = User.objects.create_user("fresh", password="x", role="DEV")
        self.assertTrue(EmployeeProfile.objects.filter(user=u).exists())

    def test_directory_needs_employees_view(self):
        self.client.login(username="pm", password="x")
        self.assertEqual(self.client.get(reverse("employees:directory")).status_code, 200)
        self.client.login(username="dev", password="x")
        self.assertEqual(self.client.get(reverse("employees:directory")).status_code, 302)

    def test_directory_search_by_skill(self):
        self.profile.skills.add(Skill.objects.create(name="Django"))
        self.client.login(username="pm", password="x")
        r = self.client.get(reverse("employees:directory"), {"q": "django"})
        self.assertContains(r, "Dev Eloper")
        r = self.client.get(reverse("employees:directory"), {"q": "cobol"})
        self.assertNotContains(r, "Dev Eloper")

    def test_basic_viewer_never_sees_sensitive(self):
        """employees.view without payroll.view: profile yes, salary/bank/docs no."""
        EmployeeDocument.objects.create(
            employee=self.profile, doc_type="PAN",
            file=SimpleUploadedFile("pan.pdf", PDF))
        self.client.login(username="pm", password="x")
        r = self.client.get(self.profile.get_absolute_url())
        self.assertContains(r, "Dev Eloper")
        self.assertContains(r, "12 Baker Street")
        self.assertNotContains(r, "55")          # salary hidden
        self.assertNotContains(r, "9876543210")  # bank hidden
        self.assertNotContains(r, "PAN card")    # docs hidden

    def test_payroll_viewer_sees_everything(self):
        EmployeeDocument.objects.create(
            employee=self.profile, doc_type="PAN",
            file=SimpleUploadedFile("pan.pdf", PDF))
        self.client.login(username="boss", password="x")
        r = self.client.get(self.profile.get_absolute_url())
        self.assertContains(r, "9876543210")
        self.assertContains(r, "PAN card")

    def test_self_can_view_own_profile_and_docs_without_role(self):
        doc = EmployeeDocument.objects.create(
            employee=self.profile, doc_type="RESUME",
            file=SimpleUploadedFile("cv.pdf", PDF))
        self.client.login(username="dev", password="x")
        r = self.client.get(reverse("employees:me"), follow=True)
        self.assertContains(r, "Resume")
        self.assertContains(r, "9876543210")  # own bank/payroll visible on own account
        dl = self.client.get(reverse("employees:document_download", args=[doc.pk]))
        self.assertEqual(dl.status_code, 200)
        # …but not someone ELSE's docs/profile
        other = EmployeeDocument.objects.create(
            employee=self.pm.employee_profile, doc_type="PAN",
            file=SimpleUploadedFile("pan.pdf", PDF))
        self.assertEqual(
            self.client.get(reverse("employees:document_download",
                                    args=[other.pk])).status_code, 302)

    def test_self_edit_basic_but_not_hr_fields(self):
        self.client.login(username="dev", password="x")
        r = self.client.post(
            reverse("employees:edit_basic", args=[self.profile.pk]),
            {"phone": "12345", "personal_email": "dev@home.in",
             "address": "New addr", "emergency_contact_name": "Mum",
             "emergency_contact_phone": "999", "skills": "Django, React"},
        )
        self.assertRedirects(r, self.profile.get_absolute_url())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.address, "New addr")
        self.assertEqual(self.profile.skills.count(), 2)
        # HR endpoint refused: salary/status/role unchanged
        r = self.client.post(
            reverse("employees:edit_hr", args=[self.profile.pk]),
            {"salary": "999999", "status": "TERMINATED"})
        self.profile.refresh_from_db()
        self.assertEqual(float(self.profile.salary), 55000.0)
        self.assertEqual(self.profile.status, "ACTIVE")

    def test_cannot_edit_someone_elses_basic_profile_without_role(self):
        self.client.login(username="dev", password="x")
        target = self.pm.employee_profile
        self.client.post(reverse("employees:edit_basic", args=[target.pk]),
                         {"phone": "0", "address": "hacked"})
        target.refresh_from_db()
        self.assertNotEqual(target.address, "hacked")

    def test_hr_can_edit_hr_fields(self):
        self.client.login(username="boss", password="x")
        r = self.client.post(
            reverse("employees:edit_hr", args=[self.profile.pk]),
            {"salary": "60000", "status": "NOTICE_PERIOD",
             "bank_account_name": "Dev E", "bank_account_number": "111",
             "bank_ifsc": "HDFC0001", "date_of_joining": "2025-01-06"})
        self.assertRedirects(r, self.profile.get_absolute_url())
        self.profile.refresh_from_db()
        self.assertEqual(float(self.profile.salary), 60000.0)
        self.assertEqual(self.profile.status, "NOTICE_PERIOD")

    def test_create_employee_makes_user_and_profile(self):
        from accounts.models import Role
        self.client.login(username="boss", password="x")
        r = self.client.post(reverse("employees:create"), {
            "username": "newbie", "password": "temp@123",
            "first_name": "New", "last_name": "Bie", "email": "n@dv.in",
            "primary_role": Role.objects.get(name="Developer").pk,
            "date_of_joining": "2026-07-20", "status": "PROBATION",
        })
        newbie = User.objects.get(username="newbie")
        profile = newbie.employee_profile
        self.assertRedirects(r, profile.get_absolute_url())
        self.assertEqual(profile.status, "PROBATION")
        self.assertEqual(newbie.primary_role.name, "Developer")
        self.assertTrue(newbie.check_password("temp@123"))
        # employees.create is enforced
        self.client.login(username="dev", password="x")
        r = self.client.post(reverse("employees:create"),
                             {"username": "nope", "password": "x"})
        self.assertFalse(User.objects.filter(username="nope").exists())

    def test_upload_hidden_rules(self):
        # PM (employees.view only) cannot upload for others
        self.client.login(username="pm", password="x")
        self.client.post(
            reverse("employees:document_upload", args=[self.profile.pk]),
            {"file": SimpleUploadedFile("x.pdf", PDF), "doc_type": "NDA"})
        self.assertEqual(self.profile.documents.count(), 0)
        # the employee can upload to their own profile
        self.client.login(username="dev", password="x")
        self.client.post(
            reverse("employees:document_upload", args=[self.profile.pk]),
            {"file": SimpleUploadedFile("cv.pdf", PDF), "doc_type": "RESUME"})
        self.assertEqual(self.profile.documents.count(), 1)
