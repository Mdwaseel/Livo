"""Assignment emails.

The rules that matter are the negative ones — who does *not* get emailed. A
system that mails everyone on every save gets muted by its users within a week,
at which point the notification is worth nothing.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Role
from clients.models import Client
from core.models import Notification
from projects.emails import (absolute_url, send_project_member_added,
                             send_task_assigned)
from projects.models import Project, Task

User = get_user_model()


def _role(name):
    return Role.objects.get(name=name)


@override_settings(TASK_ASSIGNMENT_EMAILS=True,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class TaskAssignedEmailTests(TestCase):

    def setUp(self):
        self.manager = User.objects.create_user(
            "manager", email="manager@example.com", password="x",
            first_name="Mo", last_name="Manager", primary_role=_role("Manager"))
        self.dev = User.objects.create_user(
            "dev", email="dev@example.com", password="x",
            first_name="Dee", last_name="Veloper", primary_role=_role("Developer"))
        self.client_obj = Client.objects.create(name="Acme", created_by=self.manager)
        # clients.signals auto-creates a default project per client.
        self.project = Project.objects.filter(client=self.client_obj).first()
        mail.outbox = []

    def _task(self, **kwargs):
        kwargs.setdefault("title", "Ship the landing page")
        kwargs.setdefault("project", self.project)
        return Task.objects.create(**kwargs)

    # --- the happy path -------------------------------------------------

    def test_assignee_is_emailed(self):
        task = self._task(assignee=self.dev, description="Above the fold first.")
        self.assertTrue(send_task_assigned(task, actor=self.manager))

        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["dev@example.com"])
        self.assertIn("Ship the landing page", msg.subject)
        self.assertIn("Ship the landing page", msg.body)
        self.assertIn("Mo Manager", msg.body)

    def test_email_has_a_html_alternative(self):
        task = self._task(assignee=self.dev)
        send_task_assigned(task, actor=self.manager)
        html, mime = mail.outbox[0].alternatives[0]
        self.assertEqual(mime, "text/html")
        self.assertIn("Ship the landing page", html)

    def test_body_carries_the_detail_the_assignee_needs(self):
        task = self._task(assignee=self.dev, reviewer=self.manager,
                          priority=Task.Priority.HIGH, estimated_hours=4)
        send_task_assigned(task, actor=self.manager)
        body = mail.outbox[0].body
        self.assertIn(self.project.name, body)
        self.assertIn("Acme", body)
        self.assertIn("High", body)
        self.assertIn("4 h", body)

    # --- who is deliberately NOT emailed --------------------------------

    def test_self_assignment_sends_nothing(self):
        """Nobody needs an email about a decision they just made themselves."""
        task = self._task(assignee=self.dev)
        self.assertFalse(send_task_assigned(task, actor=self.dev))
        self.assertEqual(mail.outbox, [])

    def test_unassigned_task_sends_nothing(self):
        self.assertFalse(send_task_assigned(self._task(), actor=self.manager))
        self.assertEqual(mail.outbox, [])

    def test_assignee_without_an_email_address_sends_nothing(self):
        nobody = User.objects.create_user("nomail", password="x")
        self.assertFalse(send_task_assigned(self._task(assignee=nobody),
                                            actor=self.manager))
        self.assertEqual(mail.outbox, [])

    def test_deactivated_assignee_sends_nothing(self):
        self.dev.is_active = False
        self.dev.save(update_fields=["is_active"])
        self.assertFalse(send_task_assigned(self._task(assignee=self.dev),
                                            actor=self.manager))
        self.assertEqual(mail.outbox, [])

    @override_settings(TASK_ASSIGNMENT_EMAILS=False)
    def test_feature_switch_turns_it_off(self):
        self.assertFalse(send_task_assigned(self._task(assignee=self.dev),
                                            actor=self.manager))
        self.assertEqual(mail.outbox, [])

    # --- failure must not propagate -------------------------------------

    def test_a_dead_mail_server_does_not_raise(self):
        """The task is already saved; a broken SMTP box must not undo that."""
        task = self._task(assignee=self.dev)
        with patch("core.mailer.EmailMultiAlternatives.send",
                   side_effect=OSError("connection refused")):
            with self.assertLogs("core.mailer", level="ERROR"):
                self.assertFalse(send_task_assigned(task, actor=self.manager))
        self.assertEqual(mail.outbox, [])

    # --- link building ---------------------------------------------------

    @override_settings(SITE_URL="https://za-an.com")
    def test_link_falls_back_to_site_url_without_a_request(self):
        self.assertEqual(absolute_url("/tasks/3/"), "https://za-an.com/tasks/3/")

    @override_settings(SITE_URL="")
    def test_link_is_dropped_when_there_is_no_host_to_use(self):
        """Better no button than one that 404s."""
        self.assertEqual(absolute_url("/tasks/3/"), "")
        task = self._task(assignee=self.dev)
        send_task_assigned(task)
        self.assertNotIn("Open it here", mail.outbox[0].body)


@override_settings(TASK_ASSIGNMENT_EMAILS=True,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AssignmentEmailWiringTests(TestCase):
    """Every route that can hand someone a task must send the email."""

    def setUp(self):
        self.manager = User.objects.create_user(
            "manager2", email="m2@example.com", password="pw-for-tests-1",
            primary_role=_role("Manager"))
        self.dev = User.objects.create_user(
            "dev2", email="d2@example.com", password="pw-for-tests-1",
            primary_role=_role("Developer"))
        self.client_obj = Client.objects.create(name="Beta", created_by=self.manager)
        self.project = Project.objects.filter(client=self.client_obj).first()
        self.client.force_login(self.manager)
        mail.outbox = []

    def test_creating_a_task_with_an_assignee_emails_them(self):
        self.client.post(
            reverse("projects:task_create", args=[self.project.pk]),
            {"title": "Wire the form", "assignee": self.dev.pk, "priority": "MED"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["d2@example.com"])

    def test_reassigning_from_the_task_page_emails_the_new_owner(self):
        task = Task.objects.create(project=self.project, title="Fix the header")
        mail.outbox = []
        self.client.post(reverse("projects:task_assign", args=[task.pk]),
                         {"assignee": self.dev.pk})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["d2@example.com"])

    def test_reassigning_to_the_same_person_does_not_email_again(self):
        """Saving a task without touching the assignee is not an assignment."""
        task = Task.objects.create(project=self.project, title="Fix the header",
                                   assignee=self.dev)
        mail.outbox = []
        self.client.post(reverse("projects:task_assign", args=[task.pk]),
                         {"assignee": self.dev.pk})
        self.assertEqual(mail.outbox, [])

    def test_the_email_links_back_to_the_task(self):
        self.client.post(
            reverse("projects:task_create", args=[self.project.pk]),
            {"title": "Link me", "assignee": self.dev.pk, "priority": "MED"})
        task = Task.objects.get(title="Link me")
        self.assertIn(f"http://testserver{task.get_absolute_url()}",
                      mail.outbox[0].body)


@override_settings(PROJECT_MEMBER_EMAILS=True,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ProjectMemberEmailTests(TestCase):
    """Being added to a project.

    Membership is not a label in this system — it is what makes a project
    *visible* at all (see `projects/access.py`), so being added is the moment a
    whole area of the app appears for someone. The negative rules from the
    assignment email apply unchanged: no mail for your own decision, none to a
    deactivated account, and a broken mail server never undoes the membership.
    """

    def setUp(self):
        self.manager = User.objects.create_user(
            "pm-mail", email="pm@example.com", password="pw-for-tests-1",
            first_name="Priya", last_name="Manager", primary_role=_role("Manager"))
        self.dev = User.objects.create_user(
            "dev-mail", email="dev@example.com", password="pw-for-tests-1",
            first_name="Dee", last_name="Veloper", primary_role=_role("Developer"))
        self.designer = User.objects.create_user(
            "design-mail", email="design@example.com", password="pw-for-tests-1",
            first_name="Sam", last_name="Design", primary_role=_role("Designer"))
        self.client_obj = Client.objects.create(name="Acme", created_by=self.manager)
        # clients.signals auto-creates a default project per client.
        self.project = Project.objects.filter(client=self.client_obj).first()
        self.project.name = "Website rebuild"
        self.project.status = Project.Status.ACTIVE
        self.project.save()
        mail.outbox = []

    # --- the happy path -------------------------------------------------

    def test_the_new_member_is_emailed(self):
        self.project.members.add(self.dev)
        self.assertTrue(
            send_project_member_added(self.project, self.dev, actor=self.manager))

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["dev@example.com"])
        self.assertIn("Website rebuild", message.subject)
        self.assertIn("Priya Manager", message.body)

    def test_the_body_says_what_the_project_is(self):
        self.project.members.add(self.dev, self.designer)
        send_project_member_added(self.project, self.dev, actor=self.manager)
        body = mail.outbox[0].body

        self.assertIn("Website rebuild", body)
        self.assertIn("Acme", body)          # the client
        self.assertIn("Active", body)        # the status
        self.assertIn("Sam Design", body)    # who else is on it

    def test_the_recipient_is_not_listed_as_their_own_teammate(self):
        self.project.members.add(self.dev, self.designer)
        send_project_member_added(self.project, self.dev, actor=self.manager)

        # Their name is in the greeting, so the assertion has to be about the
        # Team line specifically rather than the body as a whole.
        [team] = [line for line in mail.outbox[0].body.splitlines()
                  if line.startswith("Team:")]
        self.assertIn("Sam Design", team)
        self.assertNotIn("Dee Veloper", team)

    def test_work_already_waiting_for_them_is_counted(self):
        self.project.members.add(self.dev)
        Task.objects.create(project=self.project, title="Header", assignee=self.dev)
        Task.objects.create(project=self.project, title="Footer", assignee=self.dev,
                            status=Task.Status.DONE)
        Task.objects.create(project=self.project, title="Nav", assignee=self.designer)

        send_project_member_added(self.project, self.dev, actor=self.manager)
        # One: the done task and somebody else's task are both excluded.
        self.assertIn("1 open task", mail.outbox[0].body)

    def test_email_has_a_html_alternative(self):
        send_project_member_added(self.project, self.dev, actor=self.manager)
        html, mime = mail.outbox[0].alternatives[0]
        self.assertEqual(mime, "text/html")
        self.assertIn("Website rebuild", html)

    # --- who is deliberately NOT emailed --------------------------------

    def test_adding_yourself_sends_nothing(self):
        self.assertFalse(
            send_project_member_added(self.project, self.dev, actor=self.dev))
        self.assertEqual(mail.outbox, [])

    def test_somebody_with_no_address_sends_nothing(self):
        nobody = User.objects.create_user("nomail-p", password="pw-for-tests-1")
        self.assertFalse(
            send_project_member_added(self.project, nobody, actor=self.manager))

    def test_a_deactivated_account_sends_nothing(self):
        self.dev.is_active = False
        self.dev.save(update_fields=["is_active"])
        self.assertFalse(
            send_project_member_added(self.project, self.dev, actor=self.manager))

    @override_settings(PROJECT_MEMBER_EMAILS=False)
    def test_the_feature_switch_turns_it_off(self):
        self.assertFalse(
            send_project_member_added(self.project, self.dev, actor=self.manager))
        self.assertEqual(mail.outbox, [])

    def test_a_dead_mail_server_does_not_raise(self):
        """The membership is already granted; a broken SMTP box must not undo
        it, and the person who granted it must not see a 500."""
        with patch("core.mailer.EmailMultiAlternatives.send",
                   side_effect=OSError("connection refused")):
            with self.assertLogs("core.mailer", level="ERROR"):
                self.assertFalse(send_project_member_added(
                    self.project, self.dev, actor=self.manager))
        self.assertEqual(mail.outbox, [])


@override_settings(PROJECT_MEMBER_EMAILS=True, TASK_ASSIGNMENT_EMAILS=True,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ProjectMemberWiringTests(TestCase):
    """Does using the app actually send it?"""

    def setUp(self):
        self.manager = User.objects.create_user(
            "pm-wire", email="pm@example.com", password="pw-for-tests-1",
            primary_role=_role("Manager"))
        self.dev = User.objects.create_user(
            "dev-wire", email="dev@example.com", password="pw-for-tests-1",
            primary_role=_role("Developer"))
        self.client_obj = Client.objects.create(name="Beta", created_by=self.manager)
        self.project = Project.objects.filter(client=self.client_obj).first()
        self.client.force_login(self.manager)
        mail.outbox = []

    def _add(self, person):
        return self.client.post(
            reverse("projects:members", args=[self.project.pk]),
            {"user": person.pk})

    def test_adding_someone_from_the_team_tab_emails_them(self):
        self._add(self.dev)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["dev@example.com"])
        self.assertIn(f"http://testserver{self.project.get_absolute_url()}",
                      mail.outbox[0].body)

    def test_adding_someone_who_is_already_on_it_does_not_email_again(self):
        """Re-submitting the form is not a second addition."""
        self._add(self.dev)
        mail.outbox = []
        self._add(self.dev)
        self.assertEqual(mail.outbox, [])

    def test_removing_and_re_adding_emails_once_each_time(self):
        self._add(self.dev)
        mail.outbox = []
        self.client.post(reverse("projects:members", args=[self.project.pk]),
                         {"user": self.dev.pk, "action": "remove"})
        self.assertEqual(mail.outbox, [])
        self._add(self.dev)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_bell_notification_goes_out_alongside_the_email(self):
        self._add(self.dev)
        self.assertTrue(
            Notification.objects.filter(user=self.dev,
                                        text__contains="added to").exists())

    def test_being_assigned_a_task_does_not_also_send_a_membership_email(self):
        """Assignment already puts somebody on the project (`ensure_member`).
        Two emails about one action is how a channel gets muted — the task
        email names the project, so it is the one that goes."""
        self.client.post(
            reverse("projects:task_create", args=[self.project.pk]),
            {"title": "Wire the form", "assignee": self.dev.pk, "priority": "MED"})

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("New task", mail.outbox[0].subject)
