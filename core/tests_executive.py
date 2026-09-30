"""Executive dashboard: who gets in, what the money gate hides, and whether
the composite score can be taken apart again.

The scoring tests are the point of this file. A blended number is easy to get
subtly wrong and impossible to notice being wrong by looking at the page — a
health score of 68 looks exactly as plausible as the 74 it should have been. So
each component is asserted against a figure worked out by hand, and the
renormalisation for a finance-blind viewer is asserted as an identity rather
than as "close enough".
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import Module, Role, RolePermission
from analytics import executive
from analytics.tests.factories import Scenario

PASSWORD = "analytics-test-pass-99"
RUPEE = "₹"


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
        response = self.client.get(reverse("core:executive"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_manager_gets_the_executive_dashboard_at_the_root(self):
        """It IS the dashboard now, not a page parked on its own URL."""
        self.client.login(username="mgr", password=PASSWORD)
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/executive.html")

    def test_role_without_analytics_view_gets_the_personal_dashboard(self):
        """A Developer is not bounced off the front page -- they get the
        lighter dashboard instead. The fork is what makes one URL work for
        everybody."""
        self.client.login(username="anna", password=PASSWORD)
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/dashboard.html")
        self.assertTemplateNotUsed(response, "core/executive.html")

    def test_old_executive_url_still_forwards(self):
        """It had its own URL for exactly one release. Bookmarks from that
        release must land somewhere rather than 404."""
        self.client.login(username="mgr", password=PASSWORD)
        response = self.client.get(reverse("core:executive"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("core:dashboard"))

    def test_project_manager_can_view_after_migration_0012(self):
        """Migration 0012 grants Project Manager `analytics.view`. If that
        migration is ever reverted this is the test that says so."""
        from accounts.permissions import has_perm
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.get(username="anna")
        user.primary_role = Role.objects.get(name="Project Manager")
        user.save()
        self.assertTrue(has_perm(user, "analytics", "view"))

    def test_brief_endpoint_is_gated_too(self):
        """The JSON route carries the same gate as the page. A separate URL is
        a separate front door, and one of them being open is the whole bug."""
        self.client.login(username="anna", password=PASSWORD)
        response = self.client.get(reverse("core:executive_brief"))
        self.assertEqual(response.status_code, 302)


class FinanceGateTests(TestCase):
    """`business.view` decides whether money EXISTS on the page, not whether it
    is masked. A blanked-out tile still discloses that the figure is tracked.

    `business.view` and not `finance.view`: the finance key means "can open an
    invoice", which several roles hold and none of which should be handed the
    agency's cash position on the front page.
    """

    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()
        grant("Project Manager", "analytics", "view")
        grant("Manager", "business", "view")

    def _body(self, username):
        self.client.login(username=username, password=PASSWORD)
        html = self.client.get(reverse("core:dashboard")).content.decode()
        # Only the page body — the sidebar carries its own money links, which
        # are gated separately and are not what this test is about.
        return html.split("<main", 1)[-1]

    def test_manager_sees_money(self):
        body = self._body("mgr")
        self.assertIn("Revenue", body)
        self.assertIn("Open pipeline", body)
        self.assertIn(RUPEE, body)

    def test_finance_blind_viewer_sees_no_money_at_all(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.get(username="anna")
        user.primary_role = Role.objects.get(name="Project Manager")
        user.save()
        revoke("Project Manager", "business", "view")

        body = self._body("anna")
        self.assertNotIn(RUPEE, body)
        self.assertNotIn("Open pipeline", body)
        self.assertNotIn("Outstanding", body)
        # And the money charts are absent, not merely empty: an axis labelled
        # in rupees is itself a disclosure about what is being tracked.
        self.assertNotIn('data-chart="revenue-trend"', body)
        self.assertNotIn('data-chart="pipeline-stages"', body)

    def test_page_renders_the_chart_payload_and_the_loader(self):
        """The failure this whole exercise started from: a dashboard that
        returns 200 with no charts on it."""
        self.client.login(username="mgr", password=PASSWORD)
        html = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertIn('id="chart-configs"', html)
        self.assertIn("js/charts.js", html)
        self.assertIn("js/chart.umd.js", html)


class BusinessHealthTests(TestCase):
    """The composite, taken apart by hand."""

    def _health(self, **kwargs):
        base = {
            "projects": [{"health": 80}, {"health": 60}],   # avg 70
            "clients": [{"health": 50}],                    # avg 50
            "departments": [{"headcount": 2, "hours": Decimal("10"),
                             "utilization_percent": 80.0}],  # exactly ideal -> 100
            "finance": {"collection_percent": 40.0},
            "include_finance": True,
        }
        base.update(kwargs)
        return executive.business_health(**base)

    def test_weighted_average_matches_hand_arithmetic(self):
        # (70*35 + 50*25 + 100*20 + 40*20) / 100 = (2450+1250+2000+800)/100 = 65
        self.assertEqual(self._health()["score"], 65)

    def test_dropping_finance_renormalises_rather_than_scoring_zero(self):
        # Weights 35/25/20 sum to 80, not 100:
        # (70*35 + 50*25 + 100*20) / 80 = 5700/80 = 71.25 -> 71
        result = self._health(include_finance=False)
        self.assertEqual(result["score"], 71)
        self.assertEqual(len(result["components"]), 3)

    def test_components_carry_their_contribution_to_the_total(self):
        result = self._health()
        contributions = {c["label"]: c["contribution"]
                         for c in result["components"]}
        self.assertEqual(contributions["Delivery"], 24.5)   # 70 * 35/100
        self.assertEqual(contributions["Commercial"], 8.0)  # 40 * 20/100
        # The contributions must add up to the score they explain, or the
        # breakdown is decoration rather than a decomposition.
        self.assertAlmostEqual(sum(contributions.values()),
                               result["score"], delta=0.5)

    def test_an_axis_with_no_data_is_unscored_not_zero(self):
        """An install with no clients is not a business in poor health."""
        result = self._health(clients=[])
        customers = next(c for c in result["components"]
                         if c["label"] == "Customers")
        self.assertIsNone(customers["score"])
        self.assertEqual(customers["band"], "unknown")
        # (70*35 + 100*20 + 40*20) / 75 = 5250/75 = 70
        self.assertEqual(result["score"], 70)

    def test_no_data_anywhere_scores_none_not_zero(self):
        result = executive.business_health(
            projects=[], clients=[], departments=[], finance={},
            include_finance=False)
        self.assertIsNone(result["score"])
        self.assertEqual(result["band"], "unknown")

    def test_utilisation_is_penalised_in_both_directions(self):
        """Overwork is not excellence. 105% must score the same as 55%."""
        hot = executive._utilisation_score(
            [{"headcount": 1, "utilization_percent": 105.0}])
        cold = executive._utilisation_score(
            [{"headcount": 1, "utilization_percent": 55.0}])
        self.assertEqual(hot, cold)
        self.assertEqual(hot, 0.0)

    def test_utilisation_is_weighted_by_headcount(self):
        """A two-person team running hot must not outweigh a twenty-person
        team sitting at target."""
        score = executive._utilisation_score([
            {"headcount": 20, "utilization_percent": 80.0},   # 100
            {"headcount": 2, "utilization_percent": 105.0},   # 0
        ])
        self.assertAlmostEqual(score, 100 * 20 / 22, places=4)

    def test_bands_use_one_set_of_cutoffs(self):
        self.assertEqual(executive.band_for(70), "healthy")
        self.assertEqual(executive.band_for(69.9), "monitor")
        self.assertEqual(executive.band_for(45), "monitor")
        self.assertEqual(executive.band_for(44.9), "risk")
        self.assertEqual(executive.band_for(None), "unknown")


class AlertTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def _alerts(self, **kwargs):
        from analytics import selectors, services
        from django.test import RequestFactory

        request = RequestFactory().get("/executive/")
        request.user = self.data.manager
        filters = selectors.Filters.from_request(request)
        base = {
            "filters": filters,
            "include_finance": True,
            "projects": services.project_metrics(filters),
            "clients": services.client_metrics(filters),
            "departments": services.department_metrics(filters),
            "hours": selectors.hours_breakdown(filters),
            "task_counts": selectors.task_status_counts(filters),
        }
        base.update(kwargs)
        filters = base.pop("filters")
        return executive.alerts(filters, **base)

    def test_every_alert_names_a_resolvable_route(self):
        """A dead link on an alert card is worse than no link — the card is
        supposed to be the shortest path to the thing it is warning about."""
        from django.urls import reverse as resolve_name

        for alert in self._alerts():
            with self.subTest(alert=alert["title"]):
                self.assertTrue(resolve_name(alert["route"]))

    def test_an_empty_business_still_gets_an_all_clear_not_a_blank(self):
        alerts = self._alerts(projects=[], clients=[], departments=[],
                              hours={"pending_count": 0},
                              task_counts={"overdue": 0, "total": 0, "done": 0})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["level"], "healthy")

    def test_alerts_are_ordered_most_severe_first(self):
        order = {"critical": 0, "warning": 1, "monitor": 2, "healthy": 3}
        levels = [order[a["level"]] for a in self._alerts()]
        self.assertEqual(levels, sorted(levels))

    def test_finance_alert_is_absent_without_finance_visibility(self):
        titles = [a["title"] for a in self._alerts(include_finance=False)]
        self.assertNotIn("Delivery ahead of collection", titles)


class BriefTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_ai_failure_degrades_to_null_rather_than_500(self):
        """The one thing this page must never do is fail to load because a
        third party is having a bad morning."""
        from django.core.cache import cache

        cache.clear()
        self.client.login(username="mgr", password=PASSWORD)
        with self.settings(GROQ_API_KEYS=[]):
            response = self.client.get(reverse("core:executive_brief"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["text"])

    def test_facts_handed_to_the_model_carry_no_money_without_the_gate(self):
        """Whatever is in this dict leaves the building. It must obey the same
        gate the screen does."""
        from django.test import RequestFactory

        from core import views_executive

        request = RequestFactory().get("/executive/")
        request.user = self.data.anna          # Developer: no finance.view
        data = views_executive._rollup(request)
        facts = views_executive._facts(data)

        self.assertNotIn("revenue_this_period", facts)
        self.assertNotIn("outstanding", facts)
        self.assertNotIn("portfolio_value", facts)

    def test_facts_never_leak_client_or_employee_names(self):
        from django.test import RequestFactory

        from core import views_executive

        request = RequestFactory().get("/executive/")
        request.user = self.data.manager
        facts = views_executive._facts(views_executive._rollup(request))
        blob = str(facts)
        for name in ("Acme", "Borealis", "anna", "bilal"):
            self.assertNotIn(name, blob)


class ManagementBriefTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_brief_is_never_empty(self):
        from django.test import RequestFactory

        from core import views_executive

        request = RequestFactory().get("/executive/")
        request.user = self.data.manager
        data = views_executive._rollup(request)
        lines = executive.management_brief(
            data["filters"], include_finance=data["include_finance"],
            projects=data["projects"], clients=data["clients"],
            hours=data["hours"], task_counts=data["task_counts"],
            finance=data["finance"])
        self.assertTrue(lines)
        self.assertTrue(all(line.strip() for line in lines))


class DeltaTests(TestCase):
    """The change arrows. These are the figures most likely to be quoted out
    loud, so an absent comparison must stay absent rather than becoming 100%."""

    def test_no_previous_period_means_no_arrow(self):
        self.assertIsNone(executive.delta(500, 0))
        self.assertIsNone(executive.delta(500, None))

    def test_growth_and_decline_are_signed_by_direction_not_value(self):
        up = executive.delta(150, 100)
        self.assertEqual((up["percent"], up["direction"]), (50.0, "up"))
        down = executive.delta(50, 100)
        self.assertEqual((down["percent"], down["direction"]), (50.0, "down"))

    def test_flat_is_its_own_direction(self):
        self.assertEqual(executive.delta(100, 100)["direction"], "flat")

    def test_previous_window_is_equal_length_and_does_not_overlap(self):
        from datetime import date

        from analytics.selectors import Filters

        current = Filters(start=date(2026, 7, 1), end=date(2026, 7, 31))
        previous = executive.previous_window(current)
        self.assertEqual(previous.end, date(2026, 6, 30))
        self.assertEqual(current.end - current.start,
                         previous.end - previous.start)
        self.assertLess(previous.end, current.start)


class RecommendedActionTests(TestCase):
    def test_actions_only_ever_come_from_shown_alerts(self):
        alerts = [
            {"level": "critical", "title": "Overdue tasks", "body": "b",
             "metric": "Overdue: 4", "route": "tasks:mine"},
            {"level": "monitor", "title": "Accounts with no live work",
             "body": "b", "metric": "Dormant: 2", "route": "analytics:clients"},
        ]
        actions = executive.recommended_actions(alerts)
        self.assertEqual([a["title"] for a in actions],
                         ["Overdue tasks", "Accounts with no live work"])
        self.assertEqual([a["index"] for a in actions], [1, 2])

    def test_an_all_clear_produces_no_actions(self):
        """"Nothing needs attention" is a finding, not a task."""
        alerts = [{"level": "healthy", "title": "Nothing needs attention",
                   "body": "b", "metric": "Alerts: 0",
                   "route": "analytics:dashboard"}]
        self.assertEqual(executive.recommended_actions(alerts), [])

    def test_actions_are_capped(self):
        alerts = [{"level": "warning", "title": f"t{i}", "body": "b",
                   "metric": "m", "route": "analytics:dashboard"}
                  for i in range(9)]
        self.assertEqual(len(executive.recommended_actions(alerts)), 3)
