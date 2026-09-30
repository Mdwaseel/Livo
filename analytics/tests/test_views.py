"""View and permission tests: who gets in, what they see, and what the export
actually hands over."""
import io
import zipfile

from django.test import TestCase
from django.urls import reverse

from accounts.models import Module, Role, RolePermission

from .factories import Scenario

PASSWORD = "analytics-test-pass-99"


def grant(role_name, module_key, *actions):
    role = Role.objects.get(name=role_name)
    module = Module.objects.get(key=module_key)
    permission, _ = RolePermission.objects.get_or_create(role=role, module=module)
    for action in actions:
        setattr(permission, f"can_{action}", True)
    permission.save()


def revoke(role_name, module_key, *actions):
    role = Role.objects.get(name=role_name)
    module = Module.objects.get(key=module_key)
    permission = RolePermission.objects.filter(role=role, module=module).first()
    if not permission:
        return
    for action in actions:
        setattr(permission, f"can_{action}", False)
    permission.save()


class AccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_user_without_analytics_view_is_redirected(self):
        """Sales holds no analytics permission in the seeded matrix."""
        self.client.force_login(self.data.outsider)
        response = self.client.get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("core:dashboard"), response["Location"])

    def test_manager_can_open_every_page(self):
        self.client.force_login(self.data.manager)
        for name in ("dashboard", "employees", "projects", "clients", "finance"):
            with self.subTest(page=name):
                response = self.client.get(reverse(f"analytics:{name}"))
                self.assertEqual(response.status_code, 200)

    def test_sidebar_link_hidden_from_users_without_permission(self):
        self.client.force_login(self.data.outsider)
        body = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertNotIn('href="/analytics/"', body)

    def test_sidebar_link_shown_to_permitted_users(self):
        self.client.force_login(self.data.manager)
        body = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertIn('href="/analytics/"', body)


class FinanceGatingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        # Developer gets analytics but never finance — the split this suite is
        # actually about.
        grant("Developer", "analytics", "view")

    def test_developer_sees_delivery_analytics_without_revenue(self):
        self.client.force_login(self.data.anna)
        response = self.client.get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Billable hours", body)
        self.assertNotIn("Revenue", body)

    def test_finance_page_is_refused_without_finance_view(self):
        self.client.force_login(self.data.anna)
        response = self.client.get(reverse("analytics:finance"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("Finance analytics is restricted",
                      response.content.decode())

    def test_finance_subnav_link_is_hidden(self):
        self.client.force_login(self.data.anna)
        body = self.client.get(reverse("analytics:dashboard")).content.decode()
        self.assertNotIn(reverse("analytics:finance"), body)

    def test_project_table_omits_money_columns(self):
        self.client.force_login(self.data.anna)
        body = self.client.get(reverse("analytics:projects")).content.decode()
        self.assertIn("Completion", body)
        self.assertNotIn("<th>Budget</th>", body)

    def test_manager_sees_the_money(self):
        self.client.force_login(self.data.manager)
        body = self.client.get(reverse("analytics:dashboard")).content.decode()
        self.assertIn("Revenue", body)


class ContentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        self.client.force_login(self.data.manager)

    def test_dashboard_renders_chart_configs_as_json(self):
        response = self.client.get(reverse("analytics:dashboard"))
        self.assertContains(response, 'id="chart-configs"')
        self.assertContains(response, 'data-chart="hours-trend"')

    def test_dashboard_shows_the_fixture_totals(self):
        response = self.client.get(reverse("analytics:dashboard"))
        body = response.content.decode()
        self.assertIn("Billable hours", body)
        self.assertIn("Acme", body)  # the client table rendered

    def test_filters_narrow_the_page(self):
        response = self.client.get(
            reverse("analytics:employees"),
            {"employee": self.data.bilal.pk})
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user"], self.data.bilal)

    def test_filter_selection_is_preserved_in_the_form(self):
        response = self.client.get(reverse("analytics:employees"),
                                   {"department": self.data.design.pk})
        self.assertContains(
            response, f'value="{self.data.design.pk}" selected')

    def test_date_range_narrows_hours(self):
        response = self.client.get(reverse("analytics:dashboard"), {
            "start": self.data.today.isoformat(),
            "end": self.data.today.isoformat(),
        })
        cards = {c["label"]: c["value"] for c in response.context["cards"]}
        self.assertEqual(cards["Billable hours"], 0)

    def test_empty_result_shows_an_empty_state_not_a_blank_table(self):
        response = self.client.get(reverse("analytics:clients"),
                                   {"client": 999999})
        self.assertContains(response, "No clients match")

    def test_project_page_lists_risk_reasons(self):
        response = self.client.get(reverse("analytics:projects"))
        self.assertContains(response, "past target date")

    def test_pages_are_reachable_with_no_data_at_all(self):
        """A brand-new install must not 500 on an empty database."""
        from clients.models import Client
        from projects.models import Project, Task, WorkLogEntry
        from finance.models import Payment
        WorkLogEntry.objects.all().delete()
        Task.objects.all().delete()
        Payment.objects.all().delete()
        Project.objects.all().delete()
        Client.objects.all().delete()
        for name in ("dashboard", "employees", "projects", "clients", "finance"):
            with self.subTest(page=name):
                self.assertEqual(
                    self.client.get(reverse(f"analytics:{name}")).status_code, 200)


class DrillThroughIsReachableTests(TestCase):
    """Every clickable mark must have a link beside it that goes to the
    same place.

    A chart drill-through is `onClick` on a canvas: no focus, no tab stop, no
    announcement. It is a mouse feature. The rule on these pages is that each
    drillable chart is paired with a table carrying the same links, and this
    is the test that keeps the pairing honest — it reads the drill targets the
    server actually put in the chart config and demands each one appear as an
    href in the same response.

    Written after shipping two department charts that drilled on click with no
    keyboard path at all. Nothing failed at the time, because nothing looked.
    """

    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        self.client.force_login(self.data.manager)

    def _is_linked(self, body, url):
        """Is this URL an href on the page?

        Django escapes `&` to `&amp;` in an attribute, so the raw URL the chart
        config carries never appears verbatim in the markup. Both spellings are
        accepted rather than only the escaped one, so the test keeps working if
        a link is ever built somewhere that does not escape.
        """
        from django.utils.html import escape

        return f'href="{escape(url)}"' in body or f'href="{url}"' in body

    def _drill_targets(self, response):
        """The URLs the charts on this page will navigate to on click."""
        targets = {}
        for key, config in response.context["charts"].items():
            urls = (config.get("options") or {}).get("_drill") or []
            found = [url for url in urls if url]
            if found:
                targets[key] = found
        return targets

    def test_the_dashboard_charts_drill_somewhere(self):
        """Guards the guard: if the drill data disappeared, the assertion
        below would pass by being vacuous."""
        response = self.client.get(reverse("analytics:dashboard"))
        self.assertTrue(self._drill_targets(response),
                        "no chart on the dashboard carries drill targets")

    def test_every_dashboard_drill_target_is_also_a_link(self):
        response = self.client.get(reverse("analytics:dashboard"))
        body = response.content.decode()
        for key, urls in self._drill_targets(response).items():
            for url in urls:
                self.assertTrue(
                    self._is_linked(body, url),
                    f"chart {key!r} navigates to {url} on click, but nothing "
                    f"on the page links there — unreachable without a mouse")

    def test_every_employee_page_drill_target_is_also_a_link(self):
        response = self.client.get(reverse("analytics:employees"))
        body = response.content.decode()
        for key, urls in self._drill_targets(response).items():
            for url in urls:
                self.assertTrue(self._is_linked(body, url),
                                f"chart {key!r} drills to an unlinked {url}")


class ExportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def url(self, dataset="projects", fmt="csv"):
        return reverse("analytics:export", kwargs={"dataset": dataset, "fmt": fmt})

    def test_manager_can_download_csv(self):
        self.client.force_login(self.data.manager)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8-sig")
        self.assertIn("Website", body)
        self.assertIn("Budget", body)

    def test_manager_can_download_xlsx(self):
        self.client.force_login(self.data.manager)
        response = self.client.get(self.url(fmt="xlsx"))
        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())

    def test_export_honours_the_filters(self):
        """A download that doesn't match the screen is a support ticket."""
        self.client.force_login(self.data.manager)
        response = self.client.get(self.url("employees"),
                                   {"employee": self.data.bilal.pk})
        body = response.content.decode("utf-8-sig")
        self.assertIn("bilal", body)
        self.assertNotIn("anna", body)

    def test_view_only_user_cannot_export(self):
        """Project Manager holds analytics.view but not analytics.export."""
        grant("Project Manager", "analytics", "view")
        revoke("Project Manager", "analytics", "export")
        pm = self.data.anna
        pm.primary_role = Role.objects.get(name="Project Manager")
        pm.save()
        self.client.force_login(pm)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("analytics:dashboard"), response["Location"])

    def test_export_without_analytics_access_is_refused(self):
        self.client.force_login(self.data.outsider)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("core:dashboard"), response["Location"])

    def test_export_omits_money_for_non_finance_users(self):
        """The export must respect the same gate the page does — otherwise the
        CSV is the way around it."""
        grant("Developer", "analytics", "view", "export")
        self.client.force_login(self.data.anna)
        body = self.client.get(self.url()).content.decode("utf-8-sig")
        self.assertNotIn("Budget", body)
        self.assertIn("Completion", body)

    def test_unknown_dataset_is_a_404_from_the_router(self):
        self.client.force_login(self.data.manager)
        self.assertEqual(
            self.client.get("/analytics/export/salaries.csv").status_code, 404)

    def test_unknown_format_is_a_404_from_the_router(self):
        self.client.force_login(self.data.manager)
        self.assertEqual(
            self.client.get("/analytics/export/projects.pdf").status_code, 404)

    def test_export_is_recorded_in_the_activity_log(self):
        """Productivity data leaving the building is worth an audit row."""
        from core.models import ActivityLog

        self.client.force_login(self.data.manager)
        self.client.get(self.url("employees"))
        entry = ActivityLog.objects.filter(verb="exported analytics").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.user, self.data.manager)
        self.assertIn("Employees", entry.target)

    def test_all_four_datasets_export(self):
        self.client.force_login(self.data.manager)
        for dataset in ("employees", "projects", "clients", "departments"):
            for fmt in ("csv", "xlsx"):
                with self.subTest(dataset=dataset, fmt=fmt):
                    response = self.client.get(self.url(dataset, fmt))
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.content)
