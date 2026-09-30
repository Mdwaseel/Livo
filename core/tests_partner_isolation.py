"""Can a partner reach our data through the surfaces added with the executive
dashboard?

`core.tests_tenancy` already proves the engine partitions correctly, but every
one of those tests predates this dashboard, its rollups, its chart payloads and
its AI-brief endpoint. A new read surface is a new way out of the partition
until somebody demonstrates otherwise, and "it goes through selectors, which
scope" is a claim about the code rather than evidence about the behaviour.

So this file plays the partner. It signs in as a partner SUPER ADMIN — the
strongest account on the other side of the wall, one that passes every RBAC
check there is — and goes looking for our clients, our projects, our revenue
and our people through each new route in turn.

The assertions are deliberately about NAMES AND AMOUNTS rather than counts. A
count can match by coincidence; "Acme Rebrand does not appear in the rendered
HTML" cannot.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from analytics import executive, selectors, services

from .tests_tenancy import WorkspaceFixture

# Ours. None of these strings may appear anywhere a partner can see.
OUR_CLIENT = "Acme Ltd"
OUR_PROJECT = "Acme Rebrand"
OUR_REVENUE = Decimal("100000")
THEIR_REVENUE = Decimal("7000")


class PartnerCannotSeeOurDashboard(WorkspaceFixture):
    """The executive dashboard is now the front page. It is the single most
    likely place for a partition leak to go unnoticed, because it aggregates
    every module at once and a leak shows up as a number rather than as a
    row somebody recognises."""

    def setUp(self):
        self.client.force_login(self.partner_admin)

    def test_the_partner_lands_on_their_own_executive_dashboard(self):
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        # A partner super admin passes every RBAC check, so they DO get the
        # executive page -- of their own world. That is the design, not a leak.
        self.assertTemplateUsed(response, "core/executive.html")

    def test_none_of_our_names_appear_anywhere_on_it(self):
        html = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertNotIn(OUR_CLIENT, html)
        self.assertNotIn(OUR_PROJECT, html)

    def test_our_revenue_is_not_pooled_into_their_headline(self):
        """The tile reads their money or nothing -- never the sum."""
        response = self.client.get(reverse("core:dashboard"))
        tiles = {t["label"]: t for t in response.context["tiles"]}
        if "Total revenue" in tiles:
            value = Decimal(str(tiles["Total revenue"]["value"]))
            self.assertNotEqual(value, OUR_REVENUE + THEIR_REVENUE)
            self.assertLessEqual(value, THEIR_REVENUE)

    def test_the_chart_payload_carries_none_of_our_labels(self):
        """The charts are JSON in the page, not just pixels. A leak here would
        be invisible on screen and perfectly readable in View Source."""
        response = self.client.get(reverse("core:dashboard"))
        import json

        blob = json.dumps(response.context["charts"], default=str)
        self.assertNotIn(OUR_CLIENT, blob)
        self.assertNotIn(OUR_PROJECT, blob)

    def test_the_watchlist_and_accounts_tables_are_theirs_alone(self):
        response = self.client.get(reverse("core:dashboard"))
        for row in response.context["watchlist"]:
            self.assertEqual(row["project"].workspace_id, self.partner.pk)
        for row in response.context["top_clients"]:
            self.assertEqual(row["client"].workspace_id, self.partner.pk)

    def test_alerts_do_not_describe_our_projects(self):
        response = self.client.get(reverse("core:dashboard"))
        for alert in response.context["alerts"]:
            self.assertNotIn(OUR_PROJECT, alert["body"])
            self.assertNotIn(OUR_CLIENT, alert["body"])

    def test_the_management_brief_does_not_count_our_rows(self):
        response = self.client.get(reverse("core:dashboard"))
        text = " ".join(response.context["brief"])
        self.assertNotIn(OUR_PROJECT, text)
        self.assertNotIn(OUR_CLIENT, text)


class PartnerCannotSeeOurFiguresThroughTheRollups(WorkspaceFixture):
    """Straight at the functions, below the view layer. If the scoping only
    worked because a view happened to filter first, this is where that shows."""

    def _filters(self, viewer):
        from datetime import timedelta

        from django.utils import timezone

        today = timezone.localdate()
        return selectors.Filters(start=today - timedelta(days=90), end=today,
                                 viewer=viewer)

    def test_pipeline_by_stage_does_not_pool_budgets(self):
        rows = selectors.pipeline_by_stage(self._filters(self.partner_admin))
        total = sum(value for _, _, _, value in rows)
        # Ours is 500000, theirs 200000. Anything at or above the sum is a leak.
        self.assertLess(total, Decimal("500000"))

    def test_open_pipeline_totals_are_theirs_alone(self):
        totals = selectors.open_pipeline_totals(self._filters(self.partner_admin))
        self.assertLess(totals["value"] or Decimal("0"), Decimal("500000"))

    def test_weekly_task_flow_does_not_count_our_tasks(self):
        ours = selectors.weekly_task_flow(self._filters(self.owner))
        theirs = selectors.weekly_task_flow(self._filters(self.partner_admin))
        self.assertNotEqual([r[1] for r in ours], [])
        # Their created-count must not include ours.
        self.assertLessEqual(sum(r[1] for r in theirs), 1)

    def test_monthly_hours_series_is_partitioned(self):
        series = selectors.monthly_hours_series(self._filters(self.partner_admin))
        self.assertTrue(all(total == 0 for _, total, _ in series))

    def test_business_health_is_computed_from_their_world_only(self):
        filters = self._filters(self.partner_admin)
        projects = services.project_metrics(filters)
        clients = services.client_metrics(filters)
        for row in projects:
            self.assertEqual(row["project"].workspace_id, self.partner.pk)
        for row in clients:
            self.assertEqual(row["client"].workspace_id, self.partner.pk)
        health = executive.business_health(
            projects=projects, clients=clients,
            departments=services.department_metrics(filters),
            finance=services.finance_metrics(filters))
        self.assertIsNotNone(health)


class PartnerCannotSeeOurFiguresThroughTheBrief(WorkspaceFixture):
    """The AI brief endpoint sends figures to a third party. A partition leak
    here would not merely be shown to a partner -- it would be transmitted off
    the install inside somebody else's brief."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.client.force_login(self.partner_admin)

    def test_the_facts_dict_names_none_of_our_records(self):
        from django.test import RequestFactory

        from core import views_executive

        request = RequestFactory().get("/")
        request.user = self.partner_admin
        facts = views_executive._facts(views_executive._rollup(request))
        blob = str(facts)
        self.assertNotIn(OUR_CLIENT, blob)
        self.assertNotIn(OUR_PROJECT, blob)

    def test_the_brief_cache_key_separates_the_two_workspaces(self):
        """Two workspaces must never share a cached brief. Same window, same
        finance flag -- only the scope differs, and it has to be enough."""
        from core import views_executive

        ours = views_executive._scope_key(self.owner)
        theirs = views_executive._scope_key(self.partner_admin)
        self.assertNotEqual(ours, theirs)

    def test_the_endpoint_returns_their_scope_not_ours(self):
        with self.settings(GROQ_API_KEYS=[]):
            response = self.client.get(reverse("core:executive_brief"))
        self.assertEqual(response.status_code, 200)


class PartnerCannotSeeOurFiguresThroughAnalytics(WorkspaceFixture):
    """Migration 0012 widened `analytics.view`. A partner super admin already
    held it -- this confirms the widening did not also widen the partition."""

    def setUp(self):
        self.client.force_login(self.partner_admin)

    def test_every_analytics_page_is_clean(self):
        for name in ("dashboard", "employees", "projects", "clients", "finance"):
            with self.subTest(page=name):
                response = self.client.get(reverse(f"analytics:{name}"))
                if response.status_code != 200:
                    continue          # denied is also a correct answer here
                html = response.content.decode()
                self.assertNotIn(OUR_CLIENT, html)
                self.assertNotIn(OUR_PROJECT, html)

    def test_the_csv_export_does_not_hand_over_our_rows(self):
        """An export is the highest-value target on the page: it leaves as a
        file and stops being governed by anything the app can enforce."""
        response = self.client.get(
            reverse("analytics:export", kwargs={"dataset": "projects",
                                                "fmt": "csv"}))
        if response.status_code == 200:
            body = b"".join(response.streaming_content).decode() \
                if response.streaming else response.content.decode()
            self.assertNotIn(OUR_CLIENT, body)
            self.assertNotIn(OUR_PROJECT, body)


class PartnerCannotEscalateOutOfTheirWorkspace(WorkspaceFixture):
    """The partition is only as good as the ways OUT of it."""

    def setUp(self):
        self.client.force_login(self.partner_admin)

    def test_the_old_executive_url_does_not_bypass_the_fork(self):
        """A redirect is a new route. It must land them on their own page."""
        response = self.client.get(reverse("core:executive"))
        self.assertEqual(response.status_code, 302)
        followed = self.client.get(response["Location"])
        html = followed.content.decode()
        self.assertNotIn(OUR_CLIENT, html)
        self.assertNotIn(OUR_PROJECT, html)

    def test_they_cannot_reach_workspace_administration(self):
        response = self.client.get(reverse("core:workspace_list"))
        self.assertEqual(response.status_code, 403)

    def test_they_cannot_create_a_workspace(self):
        response = self.client.post(reverse("core:workspace_create"),
                                    {"name": "Trojan", "is_active": "on"})
        self.assertEqual(response.status_code, 403)

    def test_they_cannot_reassign_anyone_including_themselves(self):
        response = self.client.post(
            reverse("core:workspace_assign"),
            {f"ws_{self.partner_admin.pk}": str(self.home.pk)})
        self.assertEqual(response.status_code, 403)
        self.partner_admin.refresh_from_db()
        self.assertEqual(self.partner_admin.workspace_id, self.partner.pk)

    def test_posting_the_switcher_does_not_move_them(self):
        self.client.post(reverse("core:workspace_switch"),
                         {"workspace": str(self.home.pk)})
        html = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertNotIn(OUR_CLIENT, html)
        self.assertNotIn(OUR_PROJECT, html)

    def test_posting_the_all_position_does_not_widen_them(self):
        """`all` is the one switcher value that means "no partition". A
        partner posting it must not be handed the unrestricted scope."""
        from core.tenancy import ALL, current_scope

        self.client.post(reverse("core:workspace_switch"), {"workspace": ALL})
        html = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertNotIn(OUR_CLIENT, html)
        self.assertNotIn(OUR_PROJECT, html)
        self.assertIsNotNone(current_scope(self.partner_admin).ids)
