"""Regressions for the RBAC holes found in the July 2026 audit.

Each class below pins one bug that shipped and passed the suite at the time,
because nothing asserted the negative case.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.models import Client
from employees.models import EmployeeProfile

from .models import Role
from .permissions import is_superadmin, users_with_perm

User = get_user_model()


class ImplicitSuperAdminTests(TestCase):
    """User.role defaulted to OWNER, which the legacy shim translated into a
    Super Admin primary_role — so every user created without naming a role
    (admin's add form, a bare create_user, any signup) became a super admin."""

    def test_user_created_without_a_role_gets_no_permissions(self):
        person = User.objects.create_user("nobody", password="x")

        person.refresh_from_db()
        self.assertEqual(person.role, "")
        self.assertIsNone(person.primary_role)
        self.assertFalse(is_superadmin(person))

    def test_legacy_role_shim_still_maps_when_named_explicitly(self):
        person = User.objects.create_user("owner", password="x", role="OWNER")

        person.refresh_from_db()
        self.assertEqual(person.primary_role.name, "Super Admin")

    def test_superuser_flag_is_still_honoured(self):
        self.assertTrue(is_superadmin(User.objects.create_superuser("root", password="x")))


class HrCannotGrantRolesTests(TestCase):
    """edit_hr is reachable with employees.edit + payroll.view — which HR holds
    — and it wrote primary_role straight from POST, so HR could hand out Super
    Admin, including to themselves."""

    def setUp(self):
        self.hr = User.objects.create_user("hr", password="x")
        self.hr.primary_role = Role.objects.get(name="HR")
        self.hr.save(update_fields=["primary_role"])
        self.victim = User.objects.create_user("dev", password="x")
        self.victim.primary_role = Role.objects.get(name="Developer")
        self.victim.save(update_fields=["primary_role"])
        self.client.force_login(self.hr)

    def _post_role(self, profile, role_name):
        return self.client.post(
            reverse("employees:edit_hr", args=[profile.pk]),
            {"primary_role": Role.objects.get(name=role_name).pk, "status": "ACTIVE"},
        )

    def test_hr_cannot_promote_someone_else(self):
        profile = EmployeeProfile.objects.get(user=self.victim)

        self._post_role(profile, "Super Admin")

        self.victim.refresh_from_db()
        self.assertEqual(self.victim.primary_role.name, "Developer")

    def test_hr_cannot_promote_themselves(self):
        profile = EmployeeProfile.objects.get(user=self.hr)

        self._post_role(profile, "Super Admin")

        self.hr.refresh_from_db()
        self.assertEqual(self.hr.primary_role.name, "HR")
        self.assertFalse(is_superadmin(self.hr))

    def test_hr_can_still_edit_the_rest_of_the_record(self):
        profile = EmployeeProfile.objects.get(user=self.victim)

        self.client.post(reverse("employees:edit_hr", args=[profile.pk]),
                         {"status": "PROBATION", "salary": "50000",
                          "employee_code": "DV-9"})

        profile.refresh_from_db()
        self.assertEqual(profile.status, "PROBATION")
        self.assertEqual(str(profile.salary), "50000.00")

    def test_super_admin_can_still_assign_roles_here(self):
        admin = User.objects.create_superuser("root", password="x")
        self.client.force_login(admin)
        profile = EmployeeProfile.objects.get(user=self.victim)

        self._post_role(profile, "Project Manager")

        self.victim.refresh_from_db()
        self.assertEqual(self.victim.primary_role.name, "Project Manager")


class ReadPermissionsAreEnforcedTests(TestCase):
    """`view` was granted throughout the matrix but checked nowhere, so any
    authenticated account could read every client, project and document."""

    def setUp(self):
        self.outsider = User.objects.create_user("outsider", password="x")
        self.client_obj = Client.objects.create(name="Acme")
        self.project = self.client_obj.projects.first()  # default-project signal
        # `view` alone is no longer enough — a project is also only visible to
        # its members. Membership here isolates what this class is testing.
        self.project.members.add(self.outsider)
        self.client.force_login(self.outsider)

    def test_client_list_is_gated(self):
        self.assertEqual(self.client.get(reverse("clients:list")).status_code, 302)

    def test_client_detail_is_gated(self):
        r = self.client.get(reverse("clients:detail", args=[self.client_obj.pk]))
        self.assertEqual(r.status_code, 302)

    def test_project_detail_is_gated(self):
        r = self.client.get(reverse("projects:detail", args=[self.project.pk]))
        self.assertEqual(r.status_code, 302)

    def test_document_library_is_gated(self):
        self.assertEqual(self.client.get(reverse("documents:list")).status_code, 302)

    def test_a_role_with_view_gets_through(self):
        self.outsider.primary_role = Role.objects.get(name="Developer")
        self.outsider.save(update_fields=["primary_role"])

        self.assertEqual(
            self.client.get(reverse("projects:detail", args=[self.project.pk]))
            .status_code, 200)


class UsersWithPermTests(TestCase):
    """The inverse lookup that replaced 'loop over every user and call
    has_perm', and the deprecated role__in=["OWNER","PM"] notification query."""

    def test_finds_holders_through_primary_and_extra_roles(self):
        pm = User.objects.create_user("pm", password="x")
        pm.primary_role = Role.objects.get(name="Project Manager")
        pm.save(update_fields=["primary_role"])
        moonlighter = User.objects.create_user("extra", password="x")
        moonlighter.extra_roles.add(Role.objects.get(name="Project Manager"))
        User.objects.create_user("dev", password="x").primary_role = None

        holders = set(users_with_perm("documents", "approve")
                      .values_list("username", flat=True))

        self.assertIn("pm", holders)
        self.assertIn("extra", holders)
        self.assertNotIn("dev", holders)

    def test_excludes_inactive_users(self):
        gone = User.objects.create_user("gone", password="x", is_active=False)
        gone.primary_role = Role.objects.get(name="Project Manager")
        gone.save(update_fields=["primary_role"])

        self.assertNotIn("gone", set(users_with_perm("documents", "approve")
                                     .values_list("username", flat=True)))

    def test_rejects_an_unknown_action(self):
        with self.assertRaises(ValueError):
            users_with_perm("documents", "can_view; DROP TABLE")
