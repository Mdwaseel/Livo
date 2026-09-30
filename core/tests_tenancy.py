"""Workspace partitioning.

The question these tests exist to answer is a single one, asked from every
angle: **can a partner super admin reach anything of ours?** They hold every
RBAC action (super admins bypass `has_perm` entirely), so nothing in the
permission layer stands between them and our data — only `core.tenancy` does.

So the assertions are deliberately blunt and mostly negative. Each one names a
specific route to our data — a list page, a primary key in a URL, a chart total,
a dropdown option, a dragged calendar chip — and proves it comes back empty or
404. A leak here is not a cosmetic bug; it is the whole arrangement failing.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role
from analytics import selectors as an_selectors
from calendar_hub.models import CalendarEvent
from clients.models import Client
from core.models import ActivityLog, Workspace
from core.tenancy import (
    active_workspace_ids, home_admin_required, is_home_admin, is_home_user,
    resolve_scope, scope, workspace_for_new,
)
from documents.access import visible_documents
from documents.models import Document, DocumentType
from employees.models import EmployeeProfile
from finance.models import Payment
from projects.access import visible_projects, visible_tasks
from projects.models import Project, Task

User = get_user_model()


class WorkspaceFixture(TestCase):
    """Two agencies on one install, each with a client, project, task and money.

    Built once per class: every test below reads the same world and asserts a
    different route into it, so building it per-test would be the same six
    inserts twenty times over.
    """

    @classmethod
    def setUpTestData(cls):
        cls.home = Workspace.home()
        cls.partner = Workspace.objects.create(
            name="Northwind Partners", slug="northwind")

        superadmin = Role.objects.get(name="Super Admin")
        developer = Role.objects.get(name="Developer")

        cls.owner = User.objects.create_user(
            "owner", password="x", primary_role=superadmin, workspace=cls.home)
        cls.partner_admin = User.objects.create_user(
            "pat", password="x", primary_role=superadmin, workspace=cls.partner)
        cls.home_dev = User.objects.create_user(
            "dev", password="x", primary_role=developer, workspace=cls.home)

        cls.our_client = Client.objects.create(name="Acme Ltd", workspace=cls.home)
        cls.their_client = Client.objects.create(
            name="Contoso GmbH", workspace=cls.partner)

        cls.our_project = Project.objects.create(
            client=cls.our_client, name="Acme Rebrand", workspace=cls.home,
            budget=Decimal("500000"))
        cls.their_project = Project.objects.create(
            client=cls.their_client, name="Contoso Portal",
            workspace=cls.partner, budget=Decimal("200000"))

        cls.our_task = Task.objects.create(
            project=cls.our_project, title="Design homepage")
        cls.their_task = Task.objects.create(
            project=cls.their_project, title="Build login")

        today = timezone.localdate()
        Payment.objects.create(project=cls.our_project, amount=Decimal("100000"),
                               received_on=today)
        Payment.objects.create(project=cls.their_project, amount=Decimal("7000"),
                               received_on=today)

        cls.doc_type = DocumentType.objects.create(name="Brief", slug="brief")
        cls.our_doc = Document.objects.create(
            project=cls.our_project, document_type=cls.doc_type, title="Acme brief")
        cls.their_doc = Document.objects.create(
            project=cls.their_project, document_type=cls.doc_type,
            title="Contoso brief")

        # Creating a Client fires a signal that adds a default "<name> -
        # General" project, so each workspace holds two projects, not one. The
        # assertions below name projects rather than counting them wherever the
        # distinction matters.
        cls.our_event = CalendarEvent.objects.create(
            title="Acme standup", start_date=today, workspace=cls.home,
            created_by=cls.owner)
        cls.their_event = CalendarEvent.objects.create(
            title="Contoso kickoff", start_date=today, workspace=cls.partner,
            created_by=cls.partner_admin)


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

class ScopeResolutionTests(WorkspaceFixture):

    def test_partner_is_pinned_to_their_own_workspace(self):
        resolved = resolve_scope(self.partner_admin, session={})
        self.assertEqual(resolved.ids, frozenset({self.partner.pk}))
        self.assertEqual(resolved.workspace, self.partner)
        self.assertFalse(resolved.includes_home)

    def test_partner_cannot_switch_even_by_forging_the_session(self):
        """The session key is the switcher's state, and a partner has no
        switcher. Setting it by hand — a crafted cookie, a shared browser —
        must change nothing."""
        resolved = resolve_scope(
            self.partner_admin, session={"active_workspace": self.home.pk})
        self.assertEqual(resolved.ids, frozenset({self.partner.pk}))
        self.assertEqual(resolved.workspace, self.partner)
        self.assertFalse(resolved.includes_home)

    def test_home_staff_without_super_admin_cannot_switch(self):
        resolved = resolve_scope(
            self.home_dev, session={"active_workspace": self.partner.pk})
        self.assertEqual(resolved.ids, frozenset({self.home.pk}))

    def test_home_admin_defaults_to_home_not_everything(self):
        """The default matters as much as the capability: our own dashboard has
        to mean our own numbers unless somebody deliberately looks wider."""
        resolved = resolve_scope(self.owner, session={})
        self.assertEqual(resolved.ids, frozenset({self.home.pk}))
        self.assertEqual(resolved.workspace, self.home)
        self.assertTrue(resolved.includes_home)

    def test_home_admin_can_switch_and_go_all(self):
        resolved = resolve_scope(
            self.owner, session={"active_workspace": self.partner.pk})
        self.assertEqual(resolved.ids, frozenset({self.partner.pk}))
        self.assertEqual(resolved.workspace, self.partner)
        # Looking into a partner's world does NOT drag our unstamped rows along.
        self.assertFalse(resolved.includes_home)

        everything = resolve_scope(self.owner, session={"active_workspace": "all"})
        self.assertIsNone(everything.ids)
        self.assertIsNone(everything.workspace)

    def test_null_workspace_reads_as_home_not_as_everyone(self):
        """The failure direction that matters. A row that escaped stamping must
        fall to us, never become visible to a partner."""
        orphan = Client.objects.create(name="Legacy Co", workspace=None)
        self.assertIn(orphan, scope(Client.objects.all(), self.owner))
        self.assertNotIn(orphan, scope(Client.objects.all(), self.partner_admin))

    def test_system_context_is_unfiltered(self):
        """`user=None` outside a request is the nightly job, matching the
        convention projects.access already set."""
        self.assertIsNone(active_workspace_ids(None))

    def test_new_records_are_stamped_with_the_creators_workspace(self):
        self.assertEqual(workspace_for_new(self.partner_admin), self.partner)
        self.assertEqual(workspace_for_new(self.owner), self.home)

    def test_home_admin_flag_separates_partner_super_admins(self):
        self.assertTrue(is_home_admin(self.owner))
        self.assertFalse(is_home_admin(self.partner_admin))
        self.assertTrue(is_home_user(self.owner))
        self.assertFalse(is_home_user(self.partner_admin))


# ---------------------------------------------------------------------------
# querysets
# ---------------------------------------------------------------------------

class QuerysetScopingTests(WorkspaceFixture):

    def test_partner_super_admin_sees_only_their_projects(self):
        visible = visible_projects(Project.objects.all(), self.partner_admin)
        names = set(visible.values_list("name", flat=True))
        self.assertEqual(names,
                         {"Contoso Portal", "Contoso GmbH - General"})

    def test_view_all_does_not_cross_the_partition(self):
        """`projects.view_all` means every project in YOUR world. A partner
        holds it (super admins hold everything) and still sees one project."""
        from projects.access import can_view_all_projects
        self.assertTrue(can_view_all_projects(self.partner_admin))
        self.assertNotIn(
            self.our_project,
            visible_projects(Project.objects.all(), self.partner_admin))

    def test_tasks_documents_and_clients_all_narrow(self):
        self.assertEqual(
            list(visible_tasks(Task.objects.all(), self.partner_admin)),
            [self.their_task])
        self.assertEqual(
            list(visible_documents(Document.objects.all(), self.partner_admin)),
            [self.their_doc])
        self.assertEqual(
            list(scope(Client.objects.all(), self.partner_admin)),
            [self.their_client])

    def test_the_auto_created_default_project_lands_in_the_right_workspace(self):
        """A client's default project is made by a signal, not a view, so it
        has no request to read a workspace from — it inherits the client's.
        Getting this wrong would put a partner's first project in our lists."""
        default = Project.objects.get(name="Contoso GmbH - General")
        self.assertEqual(default.workspace, self.partner)
        self.assertNotIn(
            default, visible_projects(Project.objects.all(), self.owner))

    def test_home_admin_sees_only_ours_by_default(self):
        names = set(visible_projects(Project.objects.all(), self.owner)
                    .values_list("name", flat=True))
        self.assertEqual(names, {"Acme Rebrand", "Acme Ltd - General"})

    def test_activity_feed_is_partitioned(self):
        ActivityLog.objects.create(verb="created project", target="Acme Rebrand",
                                   workspace=self.home)
        ActivityLog.objects.create(verb="created project", target="Contoso Portal",
                                   workspace=self.partner)
        targets = list(scope(ActivityLog.objects.all(), self.partner_admin)
                       .values_list("target", flat=True))
        self.assertEqual(targets, ["Contoso Portal"])


# ---------------------------------------------------------------------------
# analytics — the numbers, not just the lists
# ---------------------------------------------------------------------------

class AnalyticsScopingTests(WorkspaceFixture):

    def _filters(self, viewer):
        today = timezone.localdate()
        return an_selectors.Filters(start=today - timedelta(days=30), end=today,
                                    viewer=viewer)

    def test_revenue_totals_do_not_pool_across_workspaces(self):
        """The specific thing the arrangement exists to prevent: our turnover
        appearing inside a partner's revenue chart."""
        theirs = an_selectors.revenue_totals(self._filters(self.partner_admin))
        ours = an_selectors.revenue_totals(self._filters(self.owner))
        self.assertEqual(theirs["received"], Decimal("7000"))
        self.assertEqual(ours["received"], Decimal("100000"))

    def test_portfolio_totals_are_per_workspace(self):
        theirs = an_selectors.portfolio_totals(self._filters(self.partner_admin))
        self.assertEqual(theirs["value"], Decimal("200000"))

    def test_filter_dropdowns_do_not_name_other_workspaces_clients(self):
        """A dropdown is a disclosure: an option naming Acme tells a partner
        Acme exists, even if selecting it returns nothing."""
        options = an_selectors.filter_options(self.partner_admin)
        self.assertEqual([c.name for c in options["clients"]], ["Contoso GmbH"])
        self.assertEqual(sorted(p.name for p in options["projects"]),
                         ["Contoso GmbH - General", "Contoso Portal"])


# ---------------------------------------------------------------------------
# calendar
# ---------------------------------------------------------------------------

class CalendarScopingTests(WorkspaceFixture):

    def test_events_are_partitioned(self):
        from calendar_hub.selectors import visible_events
        self.assertEqual(list(visible_events(self.partner_admin)),
                         [self.their_event])

    def test_dragging_someone_elses_task_is_refused(self):
        """The move endpoint takes a bare primary key, so it is the classic
        walk-the-ids route into another partition."""
        from calendar_hub.sources import get_source
        source = get_source("task")
        with self.assertRaises(LookupError):
            source.move(self.partner_admin, self.our_task.pk,
                        timezone.localdate())


# ---------------------------------------------------------------------------
# HTTP surface — primary keys in URLs
# ---------------------------------------------------------------------------

class HttpScopingTests(WorkspaceFixture):

    def setUp(self):
        self.client.force_login(self.partner_admin)

    def test_our_project_page_is_404_for_a_partner(self):
        response = self.client.get(
            reverse("projects:detail", args=[self.our_project.pk]))
        self.assertEqual(response.status_code, 404)

    def test_our_client_page_is_404_for_a_partner(self):
        response = self.client.get(
            reverse("clients:detail", args=[self.our_client.pk]))
        self.assertEqual(response.status_code, 404)

    def test_our_document_is_not_reachable(self):
        response = self.client.get(
            reverse("documents:detail", args=[self.our_doc.pk]))
        # Redirected away with a message rather than 404 — the documents module
        # has its own denial UX, and either way the content is not served.
        self.assertNotEqual(response.status_code, 200)

    def test_partner_dashboard_counts_only_their_own(self):
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        # Their own project plus their client's auto-created default.
        self.assertEqual(response.context["project_count"], 2)
        self.assertNotContains(response, "Acme Rebrand")
        self.assertNotContains(response, "Acme Ltd")

    def test_partner_cannot_open_workspace_administration(self):
        for name in ("core:workspace_list", "core:workspace_create"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)

    def test_partner_cannot_edit_shared_agency_branding(self):
        response = self.client.get(reverse("core:settings"))
        self.assertEqual(response.status_code, 403)

    def test_partner_cannot_edit_the_shared_role_matrix(self):
        response = self.client.get(reverse("accounts:role_list"))
        self.assertEqual(response.status_code, 403)

    def test_partner_team_screen_lists_only_their_own_people(self):
        """A partner IS a super admin, so the team screen opens for them. What
        it may not do is show them our staff to re-role."""
        response = self.client.get(reverse("accounts:user_list"))
        self.assertEqual(response.status_code, 200)
        usernames = {u.username for u in response.context["users"]}
        self.assertEqual(usernames, {"pat"})

    def test_partner_cannot_open_our_users_role_assignment(self):
        response = self.client.get(
            reverse("accounts:user_assign", args=[self.owner.pk]))
        self.assertEqual(response.status_code, 404)

    def test_partner_cannot_edit_shared_document_templates(self):
        response = self.client.get(reverse("doc_templates:create"))
        self.assertEqual(response.status_code, 403)

    def test_new_people_a_partner_adds_join_their_workspace(self):
        self.client.post(reverse("employees:create"), {
            "username": "newhire", "password": "s3cret-pass",
            "first_name": "New", "last_name": "Hire"})
        self.assertEqual(User.objects.get(username="newhire").workspace,
                         self.partner)

    def test_partner_cannot_move_the_switcher_by_posting_to_it(self):
        """No switcher is rendered for them, so this is a hand-crafted POST."""
        self.client.post(reverse("core:workspace_switch"),
                         {"workspace": self.home.pk})
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.context["project_count"], 2)


class HomeAdminSwitcherTests(WorkspaceFixture):

    def setUp(self):
        self.client.force_login(self.owner)

    def test_switching_shows_the_partners_numbers_and_hides_ours(self):
        self.client.post(reverse("core:workspace_switch"),
                         {"workspace": self.partner.pk})
        response = self.client.get(reverse("core:dashboard"))
        self.assertContains(response, "Contoso Portal")
        self.assertNotContains(response, "Acme Rebrand")

    def test_all_position_shows_both(self):
        self.client.post(reverse("core:workspace_switch"), {"workspace": "all"})
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.context["project_count"], 4)
        self.assertContains(response, "Acme Rebrand")
        self.assertContains(response, "Contoso Portal")

    def test_switching_back_restores_our_own_view(self):
        self.client.post(reverse("core:workspace_switch"),
                         {"workspace": self.partner.pk})
        self.client.post(reverse("core:workspace_switch"),
                         {"workspace": self.home.pk})
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.context["project_count"], 2)
        self.assertContains(response, "Acme Rebrand")
        self.assertNotContains(response, "Contoso Portal")

    def test_a_client_created_while_switched_belongs_to_the_partner(self):
        self.client.post(reverse("core:workspace_switch"),
                         {"workspace": self.partner.pk})
        self.client.post(reverse("clients:create"), {"name": "Fabrikam"})
        self.assertEqual(Client.objects.get(name="Fabrikam").workspace,
                         self.partner)

    def test_home_admin_can_administer_workspaces(self):
        self.assertEqual(
            self.client.get(reverse("core:workspace_list")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("core:workspace_create")).status_code, 200)
        self.assertEqual(
            self.client.get(
                reverse("core:workspace_edit", args=[self.partner.pk])
            ).status_code, 200)

    def test_creating_a_workspace_from_the_form(self):
        self.client.post(reverse("core:workspace_create"),
                         {"name": "Southwind Co", "color": "#ff0000",
                          "is_active": "on"})
        created = Workspace.objects.get(name="Southwind Co")
        self.assertEqual(created.slug, "southwind-co")
        self.assertFalse(created.is_home)
        self.assertTrue(created.is_active)

    def test_moving_someone_changes_what_they_see(self):
        """The assignment screen end to end: the act that hands somebody a
        different world."""
        self.client.post(reverse("core:workspace_assign"),
                         {f"ws_{self.home_dev.pk}": str(self.partner.pk)})
        self.home_dev.refresh_from_db()
        self.assertEqual(self.home_dev.workspace, self.partner)

    def test_an_admin_cannot_move_themselves_out_of_home(self):
        """Doing so would revoke the very screen they did it from, leaving the
        install with no one who can administer workspaces."""
        self.client.post(reverse("core:workspace_assign"),
                         {f"ws_{self.owner.pk}": str(self.partner.pk)})
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.workspace, self.home)

    def test_only_one_workspace_can_be_home(self):
        """A second home workspace would quietly hand its people every
        unstamped row in the database."""
        self.partner.is_home = True
        self.partner.save()
        self.home.refresh_from_db()
        self.assertFalse(self.home.is_home)
        self.assertEqual(Workspace.objects.filter(is_home=True).count(), 1)


# ---------------------------------------------------------------------------
# the shared surfaces — what deliberately does NOT partition
# ---------------------------------------------------------------------------

class SharedSurfaceTests(WorkspaceFixture):

    def test_employee_directory_is_shared_across_workspaces(self):
        """A deliberate exception, chosen when the feature was specified: one
        company directory, everybody visible in it."""
        EmployeeProfile.objects.get_or_create(user=self.owner)
        self.client.force_login(self.partner_admin)
        response = self.client.get(reverse("employees:directory"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "owner")

    def test_payroll_stays_inside_a_workspace(self):
        """The exception to the exception. The directory is shared; salary is
        not — and a partner super admin bypasses `payroll.view`, so this can
        only be held by the workspace check."""
        from employees.views import _can_view_sensitive

        ours, _ = EmployeeProfile.objects.get_or_create(user=self.owner)
        ours.salary = Decimal("900000")
        ours.save()
        theirs, _ = EmployeeProfile.objects.get_or_create(user=self.partner_admin)

        self.assertFalse(_can_view_sensitive(self.partner_admin, ours))
        # ...but they still run payroll for their own people.
        self.assertTrue(_can_view_sensitive(self.partner_admin, theirs))
        # ...and our own admin is unaffected.
        self.assertTrue(_can_view_sensitive(self.owner, ours))

    def test_partner_cannot_open_our_employees_salary_page(self):
        ours, _ = EmployeeProfile.objects.get_or_create(user=self.owner)
        self.client.force_login(self.partner_admin)
        response = self.client.get(reverse("employees:detail", args=[ours.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["show_sensitive"])
