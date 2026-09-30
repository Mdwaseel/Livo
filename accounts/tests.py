"""Template-rendering regressions for the RBAC screens.

Both classes below guard bugs that only surface at render time, so they assert
against real responses rather than view internals.
"""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Department, Designation

User = get_user_model()


class TeamPageRenderTests(TestCase):
    """`{{ x.y|default:x.z }}` raises VariableDoesNotExist when x is None: the
    main variable fails silently, but the *filter argument* does not. The team
    page hit this on every user without a manager."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            "admin", password="x", first_name="Ada")

    def test_renders_when_users_have_no_reporting_line(self):
        User.objects.create_user("solo", password="x", first_name="Sol")
        self.client.login(username="admin", password="x")

        r = self.client.get(reverse("accounts:user_list"))

        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Sol")

    def test_renders_with_full_org_data(self):
        dept = Department.objects.create(name="Delivery")
        desig = Designation.objects.create(name="Engineer", department=dept)
        manager = User.objects.create_user("mgr", password="x", first_name="Mia")
        User.objects.create_user("rep", password="x", first_name="Rey",
                                 department=dept, designation=desig,
                                 reports_to=manager)
        self.client.login(username="admin", password="x")

        r = self.client.get(reverse("accounts:user_list"))

        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Rey")
        self.assertContains(r, "Mia")      # the reporting line resolved
        self.assertContains(r, "Delivery")

    def test_user_without_a_name_falls_back_to_username(self):
        User.objects.create_user("nameless", password="x")
        self.client.login(username="admin", password="x")

        r = self.client.get(reverse("accounts:user_list"))

        self.assertContains(r, "nameless")


class LogoutTests(TestCase):
    """Django 5 removed GET support from LogoutView, so a plain <a href> to it
    returns 405 and the user stays signed in."""

    def setUp(self):
        self.user = User.objects.create_user("dana", password="x")

    def test_post_logs_the_user_out(self):
        self.client.login(username="dana", password="x")

        r = self.client.post(reverse("accounts:logout"))

        self.assertRedirects(r, reverse("accounts:login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_get_is_not_allowed(self):
        self.client.login(username="dana", password="x")

        r = self.client.get(reverse("accounts:logout"))

        self.assertEqual(r.status_code, 405)
        # The session survives, which is exactly why the link had to change.
        self.assertIn("_auth_user_id", self.client.session)

    def test_sidebar_signs_out_by_post_not_a_link(self):
        self.client.login(username="dana", password="x")
        logout_url = reverse("accounts:logout")

        body = self.client.get(reverse("core:dashboard")).content.decode()

        self.assertIn(f'action="{logout_url}"', body)
        self.assertNotIn(f'href="{logout_url}"', body)


class RowEditStylingTests(TestCase):
    """The department/designation tables edit in place; their controls need the
    row-edit class or they fall back to unstyled browser widgets."""

    def setUp(self):
        self.admin = User.objects.create_superuser("admin", password="x")
        self.dept = Department.objects.create(name="Tech")
        Designation.objects.create(name="Senior Developer", department=self.dept,
                                   level=1)
        self.client.login(username="admin", password="x")

    def test_department_rows_use_the_styled_table(self):
        r = self.client.get(reverse("accounts:department_list"))

        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'class="row-edit"')
        self.assertContains(r, "Tech")
        # Save/Delete no longer lean on an inline colour override.
        self.assertContains(r, 'class="link-act"')
        self.assertNotContains(r, 'style="color:var(--primary)"')

    def test_designation_rows_use_the_styled_table(self):
        r = self.client.get(reverse("accounts:designation_list"))

        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'class="row-edit"')
        self.assertContains(r, "Senior Developer")
        self.assertContains(r, "w-narrow")

    def test_inline_controls_stay_wired_to_their_row_form(self):
        """Controls sit outside the <form> and join it via form="…" — if that
        attribute is lost, edits silently post nothing."""
        r = self.client.get(reverse("accounts:department_list"))
        self.assertContains(r, f'form="dept-{self.dept.pk}"')

    def test_editing_a_department_still_saves(self):
        r = self.client.post(
            reverse("accounts:department_update", args=[self.dept.pk]),
            {"name": "Engineering", "head": self.admin.pk})

        self.assertEqual(r.status_code, 302)
        self.dept.refresh_from_db()
        self.assertEqual(self.dept.name, "Engineering")
        self.assertEqual(self.dept.head, self.admin)

    def test_every_inline_control_has_a_label(self):
        """Icon-free inputs in a table have no visible label, so each needs an
        explicit one for screen readers."""
        body = self.client.get(reverse("accounts:designation_list")).content.decode()
        self.assertIn("Designation title", body)
        self.assertIn("Seniority level for", body)


class TemplateCommentHygieneTests(TestCase):
    """Django's `{# ... #}` only tokenises on a single line — a wrapped one is
    not a comment and renders onto the page as visible text. Nothing catches
    this at import or check time, so scan the templates directly."""

    def test_no_multiline_hash_comments(self):
        offenders = []
        for root in settings.TEMPLATES[0]["DIRS"]:
            for path in Path(root).rglob("*.html"):
                text = path.read_text(encoding="utf-8")
                for match in re.finditer(r"\{#", text):
                    tail = text[match.start():]
                    close = tail.find("#}")
                    if close == -1 or "\n" in tail[:close]:
                        line = text[:match.start()].count("\n") + 1
                        offenders.append(f"{path.name}:{line}")

        self.assertEqual(
            offenders, [],
            "Multi-line {# #} comments render as page text — "
            "use {% comment %}...{% endcomment %} instead: "
            + ", ".join(offenders))
