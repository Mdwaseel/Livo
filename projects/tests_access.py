"""Row-level visibility: project membership and per-assignee tasks.

These tests are written from the attacker's side. A hidden button proves
nothing — every case below reaches for the URL directly, because that is how the
restriction actually gets tested in the wild.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import Module, Role, RolePermission
from calendar_hub.events import CalendarQuery
from calendar_hub.sources.tasks import TaskSource
from clients.models import Client as ClientModel
from documents.models import Document, DocumentType
from projects.access import (
    ensure_member, user_can_see_project, user_can_see_task,
    visible_projects, visible_tasks,
)
from projects.models import Project, Task

User = get_user_model()


def _role(name):
    return Role.objects.get(name=name)


def _grant(role_name, module_key, **flags):
    module = Module.objects.get(key=module_key)
    rp, _ = RolePermission.objects.get_or_create(
        role=_role(role_name), module=module)
    for flag, value in flags.items():
        setattr(rp, flag, value)
    rp.save()
    return rp


class VisibilityFixture(TestCase):
    """Two projects, two developers, one manager."""

    def setUp(self):
        self.manager = User.objects.create_user(
            "boss", email="boss@example.com", password="pw-for-tests-1",
            primary_role=_role("Manager"))
        self.dev = User.objects.create_user(
            "dev", email="dev@example.com", password="pw-for-tests-1",
            primary_role=_role("Developer"))
        self.other = User.objects.create_user(
            "other", email="other@example.com", password="pw-for-tests-1",
            primary_role=_role("Developer"))

        self.client_obj = ClientModel.objects.create(name="Acme",
                                                     created_by=self.manager)
        # clients.signals auto-creates a default project per client.
        self.mine = Project.objects.filter(client=self.client_obj).first()
        self.mine.name = "Mine"
        self.mine.save(update_fields=["name"])
        self.theirs = Project.objects.create(
            client=self.client_obj, name="Theirs", created_by=self.manager)

        self.mine.members.add(self.dev)
        self.theirs.members.add(self.other)

        self.my_task = Task.objects.create(
            project=self.mine, title="My task", assignee=self.dev)
        self.their_task = Task.objects.create(
            project=self.mine, title="Someone else's task", assignee=self.other)
        self.open_task = Task.objects.create(
            project=self.mine, title="Nobody's task")


class ProjectVisibilityTests(VisibilityFixture):

    def test_member_sees_their_project(self):
        self.assertIn(self.mine, visible_projects(Project.objects.all(), self.dev))

    def test_non_member_does_not(self):
        self.assertNotIn(self.theirs,
                         visible_projects(Project.objects.all(), self.dev))

    def test_manager_sees_every_project(self):
        self.assertEqual(
            visible_projects(Project.objects.all(), self.manager).count(),
            Project.objects.count())

    def test_project_page_is_404_for_a_non_member(self):
        """Not a redirect — a project you're not on should not be shown to exist."""
        self.client.force_login(self.dev)
        self.assertEqual(
            self.client.get(reverse("projects:detail",
                                    args=[self.theirs.pk])).status_code, 404)

    def test_project_page_opens_for_a_member(self):
        self.client.force_login(self.dev)
        self.assertEqual(
            self.client.get(reverse("projects:detail",
                                    args=[self.mine.pk])).status_code, 200)

    def test_list_page_hides_projects_you_are_not_on(self):
        self.client.force_login(self.dev)
        body = self.client.get(reverse("projects:list")).content.decode()
        self.assertIn("Mine", body)
        self.assertNotIn("Theirs", body)

    def test_system_context_is_unfiltered(self):
        """user=None is a background job, not a person being restricted."""
        self.assertEqual(visible_projects(Project.objects.all(), None).count(),
                         Project.objects.count())


class TaskVisibilityTests(VisibilityFixture):

    def _titles(self, user):
        return set(visible_tasks(Task.objects.all(), user)
                   .values_list("title", flat=True))

    def test_assignee_sees_their_own_task(self):
        self.assertIn("My task", self._titles(self.dev))

    def test_a_colleagues_task_on_a_shared_project_is_hidden(self):
        """The whole point: same project, different owner, not visible."""
        self.assertNotIn("Someone else's task", self._titles(self.dev))

    def test_unassigned_tasks_stay_visible_to_the_project(self):
        self.assertIn("Nobody's task", self._titles(self.dev))

    def test_reviewer_can_see_what_they_must_review(self):
        task = Task.objects.create(project=self.mine, title="Needs review",
                                   assignee=self.other, reviewer=self.dev)
        self.assertIn(task.title, self._titles(self.dev))

    def test_creator_can_see_what_they_raised(self):
        task = Task.objects.create(project=self.mine, title="I raised this",
                                   assignee=self.other, created_by=self.dev)
        self.assertIn(task.title, self._titles(self.dev))

    def test_the_unassigned_backlog_needs_membership(self):
        """`other` is assigned work on this project but was never added to it,
        so they see their own task and not the open backlog."""
        titles = self._titles(self.other)
        self.assertIn("Someone else's task", titles)
        self.assertNotIn("Nobody's task", titles)

    def test_an_outsider_sees_nothing_at_all(self):
        outsider = User.objects.create_user("outsider", password="pw-for-tests-1",
                                            primary_role=_role("Developer"))
        self.assertEqual(self._titles(outsider), set())

    def test_your_own_task_survives_losing_membership(self):
        """Being handed work is itself the grant — removing someone from the
        project must not orphan the task they are still expected to finish."""
        self.mine.members.remove(self.dev)
        self.assertIn("My task", self._titles(self.dev))

    def test_manager_sees_every_task(self):
        self.assertEqual(
            visible_tasks(Task.objects.all(), self.manager).count(),
            Task.objects.count())

    def test_task_page_is_404_for_someone_elses_task(self):
        self.client.force_login(self.dev)
        self.assertEqual(
            self.client.get(reverse("projects:task_detail",
                                    args=[self.their_task.pk])).status_code, 404)

    def test_write_endpoints_are_closed_too(self):
        """task_set_status carries no module decorator — _get_task is the gate."""
        self.client.force_login(self.dev)
        response = self.client.post(
            reverse("projects:task_set_status", args=[self.their_task.pk]),
            {"status": Task.Status.DONE})
        self.assertEqual(response.status_code, 404)
        self.their_task.refresh_from_db()
        self.assertNotEqual(self.their_task.status, Task.Status.DONE)

    def test_board_hides_a_colleagues_card(self):
        self.client.force_login(self.dev)
        body = self.client.get(
            reverse("projects:detail", args=[self.mine.pk])).content.decode()
        self.assertIn("My task", body)
        self.assertNotIn("Someone else&#x27;s task", body)


class BypassIsAPermissionTests(VisibilityFixture):

    def test_granting_view_all_widens_a_developer_without_a_code_change(self):
        self.assertNotIn(self.theirs,
                         visible_projects(Project.objects.all(), self.dev))
        _grant("Developer", "projects", can_view=True, can_view_all=True)
        _grant("Developer", "tasks", can_view=True, can_view_all=True)
        fresh = User.objects.get(pk=self.dev.pk)   # has_perm memoises per instance
        self.assertIn(self.theirs, visible_projects(Project.objects.all(), fresh))
        self.assertEqual(visible_tasks(Task.objects.all(), fresh).count(),
                         Task.objects.count())

    def test_defaults_keep_project_managers_scoped(self):
        pm = User.objects.create_user("pm", password="pw-for-tests-1",
                                      primary_role=_role("Project Manager"))
        self.assertEqual(visible_projects(Project.objects.all(), pm).count(), 0)


class MembershipManagementTests(VisibilityFixture):

    def test_manager_can_add_someone(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("projects:members", args=[self.theirs.pk]),
                         {"user": self.dev.pk})
        self.assertTrue(self.theirs.members.filter(pk=self.dev.pk).exists())

    def test_manager_can_remove_someone(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("projects:members", args=[self.mine.pk]),
                         {"user": self.dev.pk, "action": "remove"})
        self.assertFalse(self.mine.members.filter(pk=self.dev.pk).exists())

    def test_a_developer_cannot_add_themselves_to_a_project(self):
        self.client.force_login(self.dev)
        self.client.post(reverse("projects:members", args=[self.theirs.pk]),
                         {"user": self.dev.pk})
        self.assertFalse(self.theirs.members.filter(pk=self.dev.pk).exists())

    def test_ensure_member_is_idempotent(self):
        self.assertFalse(ensure_member(self.mine, self.dev))
        self.assertTrue(ensure_member(self.theirs, self.dev))
        self.assertFalse(ensure_member(self.theirs, self.dev))


class AssignmentGrantsAccessTests(VisibilityFixture):
    """Handing someone work must not hand them a 404."""

    def test_assigning_adds_the_person_to_the_project(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("projects:task_create", args=[self.theirs.pk]),
                         {"title": "New work", "assignee": self.dev.pk,
                          "priority": "MED"})
        self.assertTrue(self.theirs.members.filter(pk=self.dev.pk).exists())

    def test_and_the_task_is_then_reachable(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("projects:task_create", args=[self.theirs.pk]),
                         {"title": "New work", "assignee": self.dev.pk,
                          "priority": "MED"})
        task = Task.objects.get(title="New work")
        self.client.force_login(self.dev)
        self.assertEqual(
            self.client.get(reverse("projects:task_detail",
                                    args=[task.pk])).status_code, 200)


class DocumentsInheritProjectVisibilityTests(VisibilityFixture):

    def setUp(self):
        super().setUp()
        self.doc_type = DocumentType.objects.create(
            name="Contract", slug="contract", is_active=True)
        self.doc = Document.objects.create(
            project=self.theirs, document_type=self.doc_type, title="Their contract",
            created_by=self.manager)

    def test_a_document_on_a_hidden_project_is_not_reachable(self):
        self.client.force_login(self.dev)
        response = self.client.get(
            reverse("documents:detail", args=[self.doc.pk]), follow=True)
        self.assertNotContains(response, "Their contract", status_code=200)

    def test_the_library_does_not_list_it(self):
        self.client.force_login(self.dev)
        body = self.client.get(reverse("documents:list")).content.decode()
        self.assertNotIn("Their contract", body)


class CalendarRespectsVisibilityTests(VisibilityFixture):

    def _keys(self, viewer):
        from datetime import timedelta
        from django.utils import timezone
        today = timezone.localdate()
        for task in (self.my_task, self.their_task):
            task.due_date = today
            task.save(update_fields=["due_date"])
        query = CalendarQuery(start=today - timedelta(days=1),
                              end=today + timedelta(days=1), viewer=viewer)
        return {event.key for event in TaskSource().fetch(query)}

    def test_calendar_hides_a_colleagues_deadline(self):
        keys = self._keys(self.dev)
        self.assertIn(f"task:{self.my_task.pk}", keys)
        self.assertNotIn(f"task:{self.their_task.pk}", keys)

    def test_the_nightly_job_still_sees_everything(self):
        """reminders.py passes viewer=None; filtering it would break the cron."""
        keys = self._keys(None)
        self.assertIn(f"task:{self.my_task.pk}", keys)
        self.assertIn(f"task:{self.their_task.pk}", keys)


class SingleObjectHelpersTests(VisibilityFixture):

    def test_user_can_see_project(self):
        self.assertTrue(user_can_see_project(self.mine, self.dev))
        self.assertFalse(user_can_see_project(self.theirs, self.dev))
        self.assertTrue(user_can_see_project(self.theirs, self.manager))
        self.assertTrue(user_can_see_project(self.theirs, None))

    def test_user_can_see_task(self):
        self.assertTrue(user_can_see_task(self.my_task, self.dev))
        self.assertFalse(user_can_see_task(self.their_task, self.dev))
        self.assertTrue(user_can_see_task(self.open_task, self.dev))
        self.assertTrue(user_can_see_task(self.their_task, self.manager))


class ReportingModulesAreManagerOnlyTests(VisibilityFixture):

    def test_developer_is_bounced_from_analytics_and_planning(self):
        self.client.force_login(self.dev)
        for name in ("analytics:dashboard", "resource_planner:overview"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302,
                             msg=f"{name} should redirect a Developer")

    def test_manager_still_gets_in(self):
        self.client.force_login(self.manager)
        for name in ("analytics:dashboard", "resource_planner:overview"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200,
                             msg=f"{name} should open for a Manager")
