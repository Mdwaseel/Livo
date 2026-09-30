"""The confidential money view, and the dashboard it was taken off.

Two things are being guarded here, and only one of them is about the new page.

The first is a *removal*: the dashboard must not quote money any more. That is
easy to reintroduce by accident — somebody adds a card, or a context key comes
back — and the whole reason the Business page exists is that the dashboard is
the screen that ends up on a projector.

The second is that `business.view` is genuinely narrower than `finance.view`.
Reusing the finance key would have moved the page without changing who could
read it, which would have looked like progress and been none.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import Module, Role, RolePermission
from analytics import charts
from clients.models import Client
from core.models import Workspace
from finance.models import Payment
from projects.models import Project

User = get_user_model()


class BusinessFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            "owner", password="x",
            primary_role=Role.objects.get(name="Super Admin"))
        cls.manager = User.objects.create_user(
            "boss", password="x", primary_role=Role.objects.get(name="Manager"))
        cls.accounts = User.objects.create_user(
            "acct", password="x", primary_role=Role.objects.get(name="Accounts"))
        # Holds no analytics at all, so `/` gives them the personal dashboard.
        cls.dev = User.objects.create_user(
            "dev", password="x", primary_role=Role.objects.get(name="Developer"))

        cls.client_row = Client.objects.create(name="Acme Ltd")
        cls.project = Project.objects.create(
            client=cls.client_row, name="Acme Rebrand", budget=Decimal("500000"))
        from django.utils import timezone
        Payment.objects.create(project=cls.project, amount=Decimal("120000"),
                               received_on=timezone.localdate())


# ---------------------------------------------------------------------------
# the removal
# ---------------------------------------------------------------------------

class DashboardIsMoneyFreeTests(BusinessFixture):
    """`/` is two pages now — the executive rollup for anyone with
    `analytics.view`, the personal dashboard for everyone else. The money
    contract has to hold on BOTH, so these test both branches by name.

    The contract itself is unchanged and is the reason `business.view` exists:
    holding `finance.view` means you can open an invoice, which is emphatically
    not the same as being shown the agency's cash position. Accounts holds the
    finance key and must still see no money on the front page and no hint that
    /business/ is there.
    """

    def test_the_personal_dashboard_quotes_no_money(self):
        """A viewer without analytics gets the lighter page, and it carries no
        figures — the arrangement this module was split out to create."""
        self.client.force_login(self.dev)
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/dashboard.html")
        for key in ("received_month", "total_outstanding",
                    "outstanding_in_credit"):
            self.assertNotIn(key, response.context,
                             f"dashboard still computes {key}")
        self.assertNotContains(response, "Received this month")
        self.assertNotContains(response, "Total outstanding")

    def test_the_personal_dashboard_still_answers_the_operational_questions(self):
        self.client.force_login(self.dev)
        response = self.client.get(reverse("core:dashboard"))
        for key in ("client_count", "project_count", "document_count",
                    "pending_count"):
            self.assertIn(key, response.context)

    def test_the_owner_is_offered_the_way_through_to_the_figures(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:dashboard"))
        self.assertContains(response, reverse("core:business"))

    def test_someone_without_the_module_is_not_told_the_page_exists(self):
        self.client.force_login(self.accounts)
        response = self.client.get(reverse("core:dashboard"))
        self.assertNotContains(response, reverse("core:business"))

    def test_finance_view_alone_buys_no_money_on_the_executive_dashboard(self):
        """The regression this class is really guarding.

        Accounts holds `finance.view` and, since migration 0012, also
        `analytics.view` — so they land on the EXECUTIVE dashboard. If its
        money were gated on the finance key rather than `business.view`, this
        is the moment the agency's revenue would appear on the front page for
        a role that was deliberately excluded from it.
        """
        from core import views_executive

        self.assertTrue(views_executive.can_see_executive(self.accounts))
        self.assertFalse(views_executive.can_see_money(self.accounts))

        self.client.force_login(self.accounts)
        response = self.client.get(reverse("core:dashboard"))
        self.assertTemplateUsed(response, "core/executive.html")
        self.assertFalse(response.context["can_view_money"])
        body = response.content.decode().split("<main", 1)[-1]
        self.assertNotIn("\u20b9", body)
        self.assertNotIn("Total revenue", body)
        self.assertNotIn("Open pipeline", body)

    def test_the_owner_does_see_money_on_the_executive_dashboard(self):
        """The other half: `business.view` is what buys it, and it works."""
        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:dashboard"))
        self.assertTemplateUsed(response, "core/executive.html")
        self.assertTrue(response.context["can_view_money"])
        self.assertContains(response, "Total revenue")


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

class BusinessAccessTests(BusinessFixture):

    def test_the_owner_can_open_it(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:business"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["finance"]["revenue"],
                         Decimal("120000.00"))

    def test_a_manager_cannot(self):
        """Managers hold nearly everything; this is one of the three things
        they deliberately do not."""
        self.client.force_login(self.manager)
        response = self.client.get(reverse("core:business"))
        self.assertEqual(response.status_code, 302)

    def test_finance_view_alone_is_not_enough(self):
        """The point of a separate module. Accounts holds `finance.view`
        because they work invoices — that must not carry the agency's cash
        position with it."""
        from accounts.permissions import has_perm
        self.assertTrue(has_perm(self.accounts, "finance", "view"))
        self.assertFalse(has_perm(self.accounts, "business", "view"))
        self.client.force_login(self.accounts)
        self.assertEqual(
            self.client.get(reverse("core:business")).status_code, 302)

    def test_granting_the_module_to_a_role_opens_it(self):
        """"Named people" is a tick in the role matrix, which is how every
        other permission in this app is handed out."""
        module = Module.objects.get(key="business")
        role = Role.objects.get(name="Accounts")
        RolePermission.objects.update_or_create(
            role=role, module=module, defaults={"can_view": True})
        self.client.force_login(self.accounts)
        self.assertEqual(
            self.client.get(reverse("core:business")).status_code, 200)

    def test_the_figures_start_masked(self):
        """Safe by default: the markup ships masked and is revealed by a
        deliberate click, so a page opened mid-screenshare shows nothing."""
        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:business"))
        self.assertContains(response, "data-private")
        self.assertContains(response, "data-privacy-toggle")

    def test_nothing_is_revealed_by_the_html_itself(self):
        """The mask must be the *painted* state, not a state JavaScript
        arrives at.

        This started out backwards: the panels were served plain and a script
        at the bottom of the page blurred them, which meant the browser painted
        real revenue and un-painted it once a charting library had finished
        downloading. `is-revealed` is the class CSS keys the unblur on — if it
        is ever present in the response, the figures are readable before a
        single line of script has run, and the control has failed at the one
        moment it exists for.
        """
        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:business"))
        self.assertNotContains(response, "is-revealed=")
        html = response.content.decode()
        self.assertNotIn('class="stat-grid is-revealed"', html)

    def test_the_charting_library_cannot_delay_the_privacy_script(self):
        """Ordering is load-bearing, so it is asserted rather than trusted.

        The reveal decision has to be made before the browser blocks on
        chart.umd.js; putting the privacy script after it reintroduces exactly
        the window this page was built to close.
        """
        self.client.force_login(self.owner)
        html = self.client.get(reverse("core:business")).content.decode()
        self.assertLess(html.index("dv-business-visible"),
                        html.index("chart.umd.js"),
                        "the privacy script must precede the charting library")


class BusinessWorkspaceTests(BusinessFixture):

    def test_a_partner_admin_sees_their_own_figures_not_ours(self):
        """They hold `business.view` — super admins hold everything — so the
        workspace filter is the only thing standing between them and our
        revenue."""
        partner = Workspace.objects.create(name="Northwind", slug="northwind")
        partner_admin = User.objects.create_user(
            "pat", password="x", workspace=partner,
            primary_role=Role.objects.get(name="Super Admin"))
        self.client.force_login(partner_admin)
        response = self.client.get(reverse("core:business"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["finance"]["revenue"], Decimal("0.00"))
        self.assertNotContains(response, "Acme Ltd")


class DrillThroughIsReachableTests(BusinessFixture):
    """The same rule the analytics dashboard is held to.

    A chart that navigates on click is a mouse affordance — a canvas has no
    focus, no tab stop and nothing to announce. Every drillable chart here has
    to be paired with a table carrying the same links.

    This caught a live one: the by-client chart fell back to plotting every
    client when nothing was outstanding, so on a fully-collected month it drew
    ten bars that opened a client on click, sitting above a card that read
    "Nothing outstanding" and listed no rows at all.
    """

    def _targets(self, response):
        found = {}
        for key, config in (response.context["charts"] or {}).items():
            urls = [u for u in ((config.get("options") or {}).get("_drill") or [])
                    if u]
            if urls:
                found[key] = urls
        return found

    def test_every_drill_target_is_also_a_link(self):
        from django.utils.html import escape

        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:business"))
        body = response.content.decode()
        for key, urls in self._targets(response).items():
            for url in urls:
                self.assertTrue(
                    f'href="{escape(url)}"' in body or f'href="{url}"' in body,
                    f"chart {key!r} opens {url} on click with nothing linking "
                    f"there — unreachable without a mouse")

    def test_no_by_client_chart_when_there_is_no_table_under_it(self):
        """Everything collected: the card has no rows, so the chart goes."""
        from finance.models import Payment
        from django.utils import timezone

        Payment.objects.create(              # settle the outstanding balance
            project=self.project, amount=Decimal("380000"),
            received_on=timezone.localdate())
        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:business"))
        self.assertEqual(response.context["owing"], [])
        self.assertNotIn("client-revenue", response.context["charts"])
        self.assertNotContains(response, 'data-chart="client-revenue"')

    def test_the_chart_is_there_when_the_table_is(self):
        """Guards the guard — the assertion above must not pass by the chart
        never existing."""
        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:business"))
        self.assertTrue(response.context["owing"])
        self.assertIn("client-revenue", response.context["charts"])


class BusinessCostTests(BusinessFixture):
    """The page must not get slower as the agency gets bigger.

    Written after finding a full per-project finance aggregation being run to
    produce a count that the template never rendered — cost that was invisible
    precisely because nothing on screen depended on it. A flatness assertion
    catches that class of thing; reading the view did not.
    """

    def _clients(self, count):
        for index in range(count):
            client = Client.objects.create(name=f"Bulk {index} Ltd")
            project = Project.objects.create(
                client=client, name=f"Bulk {index}", budget=Decimal("100000"))
            from django.utils import timezone
            Payment.objects.create(project=project, amount=Decimal("40000"),
                                   received_on=timezone.localdate())

    def test_the_page_costs_the_same_with_ten_times_the_clients(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.force_login(self.owner)
        url = reverse("core:business")
        self.client.get(url)          # warm anything cached per-process

        self._clients(3)
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(self.client.get(url).status_code, 200)

        self._clients(30)
        with CaptureQueriesContext(connection) as large:
            self.assertEqual(self.client.get(url).status_code, 200)

        self.assertEqual(
            len(small), len(large),
            f"query count grew from {len(small)} to {len(large)} when the "
            f"client list did — something in the page is querying per row")


# ---------------------------------------------------------------------------
# the charts this page adds
# ---------------------------------------------------------------------------

class BusinessChartTests(TestCase):

    def test_client_revenue_groups_collected_beside_outstanding(self):
        """Grouped, not stacked: the gap between billed and collected is the
        thing being read, and stacking makes it a length you have to estimate."""
        rows = [{"client": type("C", (), {"name": "Acme"})(),
                 "revenue": Decimal("120000"), "outstanding": Decimal("380000")}]
        payload = charts.client_revenue(rows)
        self.assertEqual(len(payload["data"]["datasets"]), 2)
        self.assertEqual(payload["data"]["datasets"][0]["data"], [120000.0])
        self.assertEqual(payload["data"]["datasets"][1]["data"], [380000.0])
        self.assertNotIn("stack", payload["data"]["datasets"][0])

    def test_collection_gauge_never_plots_negative_outstanding(self):
        """Collecting more than billed is a credit, not a negative slice — a
        doughnut cannot draw one, and it would render as a gap."""
        payload = charts.collection_gauge({
            "collected_all_time": Decimal("500"), "outstanding": Decimal("-200")})
        self.assertEqual(payload["data"]["datasets"][0]["data"], [500.0, 0.0])

    def test_money_charts_are_flagged_for_the_currency_formatter(self):
        payload = charts.collection_gauge({
            "collected_all_time": Decimal("1"), "outstanding": Decimal("1")})
        self.assertTrue(payload["options"]["_currency"])
