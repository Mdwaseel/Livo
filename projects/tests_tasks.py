"""Task management: review workflow, dependencies, checklist/comments/
attachments, recurrence, and the My Tasks queue."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import Client
from core.models import Notification
from .models import (
    Project, RecurringTask, Task, TaskAttachment, TaskChecklistItem, TaskComment,
)

User = get_user_model()


class TaskWorkflowBase(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(name="Flow Co")
        self.project = Project.objects.create(client=self.client_obj, name="Site")
        # DEV → Developer role: tasks view/create/edit/delete, no assign/approve.
        self.dev = User.objects.create_user("dev", password="x", role="DEV",
                                            first_name="Dana")
        # PM → Project Manager: adds tasks.assign (still no tasks.approve, so
        # approval rights here come from being the named reviewer).
        self.pm = User.objects.create_user("pm", password="x", role="PM",
                                           first_name="Priya")
        self.project.members.add(self.dev, self.pm)

    def make_task(self, **kwargs):
        kwargs.setdefault("title", "Build homepage")
        kwargs.setdefault("assignee", self.dev)
        return Task.objects.create(project=self.project, **kwargs)


class ReviewWorkflowTests(TaskWorkflowBase):
    def test_done_with_reviewer_goes_to_submitted_not_done(self):
        task = self.make_task(reviewer=self.pm, status=Task.Status.IN_PROGRESS)
        self.client.login(username="dev", password="x")

        self.client.post(reverse("projects:task_set_status", args=[task.pk]),
                         {"status": "DONE"})

        task.refresh_from_db()
        self.assertEqual(task.approval_status, Task.Approval.SUBMITTED)
        self.assertEqual(task.status, Task.Status.IN_PROGRESS)
        self.assertIsNone(task.completed_on)
        self.assertEqual(task.board_column, "submitted")

    def test_done_without_reviewer_completes_directly(self):
        task = self.make_task()
        self.client.login(username="dev", password="x")

        self.client.post(reverse("projects:task_set_status", args=[task.pk]),
                         {"status": "DONE"})

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.DONE)
        self.assertEqual(task.completed_on, timezone.localdate())
        self.assertEqual(task.approval_status, Task.Approval.NONE)

    def test_submit_notifies_reviewer(self):
        task = self.make_task(reviewer=self.pm)
        self.client.login(username="dev", password="x")

        self.client.post(reverse("projects:task_submit", args=[task.pk]))

        note = Notification.objects.get(user=self.pm)
        self.assertIn("Review requested", note.text)
        self.assertIn(str(task.pk), note.url)

    def test_reviewer_approves_and_task_completes(self):
        task = self.make_task(reviewer=self.pm,
                              approval_status=Task.Approval.SUBMITTED,
                              status=Task.Status.IN_PROGRESS)
        self.client.login(username="pm", password="x")

        self.client.post(reverse("projects:task_approve", args=[task.pk]),
                         {"comment": "Looks good"})

        task.refresh_from_db()
        self.assertEqual(task.approval_status, Task.Approval.APPROVED)
        self.assertEqual(task.status, Task.Status.DONE)
        self.assertEqual(task.completed_on, timezone.localdate())
        self.assertEqual(task.approved_by, self.pm)
        self.assertIsNotNone(task.approved_on)
        self.assertTrue(
            Notification.objects.filter(user=self.dev,
                                        text__startswith="Approved").exists())
        self.assertTrue(task.comments.filter(body__contains="Looks good").exists())

    def test_reviewer_reopens_with_a_reason(self):
        task = self.make_task(reviewer=self.pm,
                              approval_status=Task.Approval.SUBMITTED,
                              status=Task.Status.IN_PROGRESS)
        self.client.login(username="pm", password="x")

        self.client.post(reverse("projects:task_reopen", args=[task.pk]),
                         {"comment": "Footer links are broken"})

        task.refresh_from_db()
        self.assertEqual(task.approval_status, Task.Approval.REOPENED)
        self.assertEqual(task.status, Task.Status.IN_PROGRESS)
        self.assertIsNone(task.completed_on)
        self.assertIsNone(task.approved_by)
        comment = task.comments.get()
        self.assertIn("Footer links are broken", comment.body)
        self.assertTrue(
            Notification.objects.filter(user=self.dev,
                                        text__startswith="Reopened").exists())

    def test_reopen_requires_a_reason(self):
        task = self.make_task(reviewer=self.pm,
                              approval_status=Task.Approval.SUBMITTED)
        self.client.login(username="pm", password="x")

        self.client.post(reverse("projects:task_reopen", args=[task.pk]),
                         {"comment": "   "})

        task.refresh_from_db()
        self.assertEqual(task.approval_status, Task.Approval.SUBMITTED)
        self.assertEqual(task.comments.count(), 0)

    def test_non_reviewer_cannot_approve(self):
        other = User.objects.create_user("other", password="x", role="DEV")
        task = self.make_task(reviewer=self.pm,
                              approval_status=Task.Approval.SUBMITTED)
        self.client.login(username="other", password="x")

        self.client.post(reverse("projects:task_approve", args=[task.pk]))

        task.refresh_from_db()
        self.assertEqual(task.approval_status, Task.Approval.SUBMITTED)
        self.assertNotEqual(task.status, Task.Status.DONE)


class AssignmentPermissionTests(TaskWorkflowBase):
    def test_assign_requires_the_assign_action(self):
        task = self.make_task(assignee=None)
        self.client.login(username="dev", password="x")  # Developer: no 'assign'

        self.client.post(reverse("projects:task_assign", args=[task.pk]),
                         {"assignee": self.dev.pk})

        task.refresh_from_db()
        self.assertIsNone(task.assignee_id)

    def test_pm_can_assign_and_assignee_is_notified(self):
        task = self.make_task(assignee=None)
        self.client.login(username="pm", password="x")

        self.client.post(reverse("projects:task_assign", args=[task.pk]),
                         {"assignee": self.dev.pk, "reviewer": self.pm.pk})

        task.refresh_from_db()
        self.assertEqual(task.assignee, self.dev)
        self.assertEqual(task.reviewer, self.pm)
        self.assertTrue(
            Notification.objects.filter(
                user=self.dev, text__startswith="You were assigned").exists())

    def test_assignee_without_edit_perm_can_still_drive_own_task(self):
        """A Sales user holds no tasks perms, but owning the task is enough."""
        sales = User.objects.create_user("sales", password="x", role="SALES")
        task = self.make_task(assignee=sales)
        self.client.login(username="sales", password="x")

        self.client.post(reverse("projects:task_set_status", args=[task.pk]),
                         {"status": "IN_PROGRESS"})

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.IN_PROGRESS)


class DependencyTests(TaskWorkflowBase):
    def test_open_blocker_refuses_completion(self):
        blocker = self.make_task(title="API ready")
        task = self.make_task(title="Wire up UI")
        task.blocked_by.add(blocker)
        self.client.login(username="dev", password="x")

        r = self.client.post(reverse("projects:task_set_status", args=[task.pk]),
                             {"status": "DONE"}, follow=True)

        task.refresh_from_db()
        self.assertNotEqual(task.status, Task.Status.DONE)
        self.assertContains(r, "blocked by")
        self.assertContains(r, "API ready")

    def test_completion_allowed_once_blocker_is_done(self):
        blocker = self.make_task(title="API ready")
        task = self.make_task(title="Wire up UI")
        task.blocked_by.add(blocker)

        blocker.status = Task.Status.DONE
        blocker.save(update_fields=["status"])

        self.client.login(username="dev", password="x")
        self.client.post(reverse("projects:task_set_status", args=[task.pk]),
                         {"status": "DONE"})

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.DONE)

    def test_blocked_task_cannot_be_submitted_for_review(self):
        blocker = self.make_task(title="API ready")
        task = self.make_task(title="Wire up UI", reviewer=self.pm)
        task.blocked_by.add(blocker)
        self.client.login(username="dev", password="x")

        self.client.post(reverse("projects:task_submit", args=[task.pk]))

        task.refresh_from_db()
        self.assertEqual(task.approval_status, Task.Approval.NONE)

    def test_dependency_picker_ignores_self_and_other_projects(self):
        other_project = Project.objects.create(client=self.client_obj, name="Other")
        stranger = Task.objects.create(project=other_project, title="Elsewhere")
        blocker = self.make_task(title="API ready")
        task = self.make_task(title="Wire up UI")
        self.client.login(username="dev", password="x")

        self.client.post(reverse("projects:task_deps_update", args=[task.pk]),
                         {"blocked_by": [blocker.pk, task.pk, stranger.pk]})

        self.assertEqual(list(task.blocked_by.all()), [blocker])

    def test_blocks_reverse_side(self):
        blocker = self.make_task(title="API ready")
        task = self.make_task(title="Wire up UI")
        task.blocked_by.add(blocker)
        self.assertEqual(list(blocker.blocks.all()), [task])


class ChecklistTests(TaskWorkflowBase):
    def setUp(self):
        super().setUp()
        self.task = self.make_task()
        self.client.login(username="dev", password="x")

    def test_add_tick_and_progress(self):
        self.client.post(reverse("projects:checklist_add", args=[self.task.pk]),
                         {"text": "Header done"})
        self.client.post(reverse("projects:checklist_add", args=[self.task.pk]),
                         {"text": "Footer done"})
        self.assertEqual(self.task.checklist_progress,
                         {"done": 0, "total": 2, "percent": 0})

        first = self.task.checklist.first()
        self.client.post(reverse("projects:checklist_toggle", args=[first.pk]))

        first.refresh_from_db()
        self.assertTrue(first.is_done)
        self.assertEqual(self.task.checklist_progress["done"], 1)
        self.assertEqual(self.task.checklist_progress["percent"], 50)

    def test_reorder_moves_item(self):
        for text in ("One", "Two", "Three"):
            self.client.post(reverse("projects:checklist_add", args=[self.task.pk]),
                             {"text": text})
        third = self.task.checklist.all()[2]

        self.client.post(reverse("projects:checklist_move", args=[third.pk]),
                         {"dir": "up"})

        self.assertEqual([i.text for i in self.task.checklist.all()],
                         ["One", "Three", "Two"])

    def test_delete_item(self):
        self.client.post(reverse("projects:checklist_add", args=[self.task.pk]),
                         {"text": "Temp"})
        item = self.task.checklist.get()
        self.client.post(reverse("projects:checklist_delete", args=[item.pk]))
        self.assertEqual(self.task.checklist.count(), 0)

    def test_outsider_cannot_edit_checklist(self):
        TaskChecklistItem.objects.create(task=self.task, text="Locked")
        outsider = User.objects.create_user("out", password="x", role="SALES")
        self.client.force_login(outsider)
        item = self.task.checklist.get()

        self.client.post(reverse("projects:checklist_toggle", args=[item.pk]))

        item.refresh_from_db()
        self.assertFalse(item.is_done)


class CommentAndAttachmentTests(TaskWorkflowBase):
    def test_comment_posts_and_notifies_the_other_side(self):
        task = self.make_task(reviewer=self.pm)
        self.client.login(username="dev", password="x")

        self.client.post(reverse("projects:comment_add", args=[task.pk]),
                         {"body": "Ready for a look"})

        comment = task.comments.get()
        self.assertEqual(comment.author, self.dev)
        self.assertEqual(comment.body, "Ready for a look")
        # Reviewer pinged; the author is not notified about their own comment.
        self.assertTrue(Notification.objects.filter(user=self.pm).exists())
        self.assertFalse(Notification.objects.filter(user=self.dev).exists())

    def test_author_can_delete_own_comment(self):
        task = self.make_task()
        comment = TaskComment.objects.create(task=task, author=self.dev, body="oops")
        self.client.login(username="dev", password="x")

        self.client.post(reverse("projects:comment_delete", args=[comment.pk]))

        self.assertEqual(task.comments.count(), 0)

    def test_attachment_upload_and_delete(self):
        task = self.make_task()
        self.client.login(username="dev", password="x")

        self.client.post(
            reverse("projects:attachment_upload", args=[task.pk]),
            {"file": SimpleUploadedFile("spec.txt", b"hello", "text/plain")})

        attachment = task.attachments.get()
        self.assertEqual(attachment.uploaded_by, self.dev)
        self.assertTrue(attachment.filename.startswith("spec"))
        self.assertFalse(attachment.is_image)

        self.client.post(reverse("projects:attachment_delete",
                                 args=[attachment.pk]))
        self.assertEqual(TaskAttachment.objects.count(), 0)


class RecurringTaskTests(TaskWorkflowBase):
    def test_generation_creates_task_and_advances_next_run(self):
        today = timezone.localdate()
        template = RecurringTask.objects.create(
            project=self.project, title="Weekly backup",
            assignee=self.dev, reviewer=self.pm,
            frequency=RecurringTask.Frequency.WEEKLY,
            next_run=today, estimated_hours=Decimal("1.5"),
        )

        out = StringIO()
        call_command("generate_recurring_tasks", stdout=out)

        task = Task.objects.get(title="Weekly backup")
        self.assertEqual(task.project, self.project)
        self.assertEqual(task.assignee, self.dev)
        self.assertEqual(task.reviewer, self.pm)
        self.assertEqual(task.estimated_hours, Decimal("1.50"))

        template.refresh_from_db()
        self.assertEqual(template.next_run, today + timedelta(days=7))
        self.assertGreater(template.next_run, today)
        self.assertIn("Created 1 task", out.getvalue())

    def test_second_run_same_day_is_a_no_op(self):
        RecurringTask.objects.create(
            project=self.project, title="Daily standup",
            frequency=RecurringTask.Frequency.DAILY,
            next_run=timezone.localdate(),
        )
        call_command("generate_recurring_tasks", stdout=StringIO())
        call_command("generate_recurring_tasks", stdout=StringIO())
        self.assertEqual(Task.objects.filter(title="Daily standup").count(), 1)

    def test_inactive_and_future_templates_are_skipped(self):
        today = timezone.localdate()
        RecurringTask.objects.create(project=self.project, title="Paused",
                                     next_run=today, is_active=False)
        RecurringTask.objects.create(project=self.project, title="Later",
                                     next_run=today + timedelta(days=3))
        call_command("generate_recurring_tasks", stdout=StringIO())
        self.assertEqual(Task.objects.count(), 0)

    def test_missed_runs_catch_up_past_today(self):
        """A scheduler that was down for a fortnight shouldn't fire repeatedly."""
        today = timezone.localdate()
        template = RecurringTask.objects.create(
            project=self.project, title="Weekly report",
            frequency=RecurringTask.Frequency.WEEKLY,
            next_run=today - timedelta(days=15),
        )
        call_command("generate_recurring_tasks", stdout=StringIO())
        template.refresh_from_db()
        self.assertGreater(template.next_run, today)
        self.assertEqual(Task.objects.filter(title="Weekly report").count(), 1)

    def test_monthly_clamps_to_short_months(self):
        from datetime import date
        template = RecurringTask.objects.create(
            project=self.project, title="Month end",
            frequency=RecurringTask.Frequency.MONTHLY,
            next_run=date(2027, 1, 31),
        )
        self.assertEqual(template._step(date(2027, 1, 31)), date(2027, 2, 28))

    def test_dry_run_writes_nothing(self):
        RecurringTask.objects.create(project=self.project, title="Weekly backup",
                                     next_run=timezone.localdate())
        out = StringIO()
        call_command("generate_recurring_tasks", "--dry-run", stdout=out)
        self.assertEqual(Task.objects.count(), 0)
        self.assertIn("Would create 1 task", out.getvalue())


class MyTasksPageTests(TaskWorkflowBase):
    def test_shows_own_tasks_grouped_with_overdue_flagged(self):
        today = timezone.localdate()
        self.make_task(title="Overdue item", due_date=today - timedelta(days=2))
        self.make_task(title="Running", status=Task.Status.IN_PROGRESS)
        self.make_task(title="Someone else's", assignee=self.pm)
        self.client.login(username="dev", password="x")

        r = self.client.get(reverse("tasks:mine"))

        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Overdue item")
        self.assertContains(r, "Running")
        self.assertNotContains(r, "Someone else&#x27;s")
        self.assertEqual(r.context["overdue_count"], 1)
        self.assertEqual([t.title for t in r.context["tasks_todo"]],
                         ["Overdue item"])
        self.assertEqual([t.title for t in r.context["tasks_progress"]],
                         ["Running"])
        self.assertContains(r, "1 overdue")

    def test_reviewer_sees_a_to_review_queue(self):
        self.make_task(title="Needs sign-off", reviewer=self.pm,
                       approval_status=Task.Approval.SUBMITTED)
        self.make_task(title="Not submitted", reviewer=self.pm)
        self.client.login(username="pm", password="x")

        r = self.client.get(reverse("tasks:mine"))

        self.assertEqual([t.title for t in r.context["to_review"]],
                         ["Needs sign-off"])
        self.assertContains(r, "To review")

    def test_empty_state_when_nothing_assigned(self):
        self.client.login(username="pm", password="x")
        r = self.client.get(reverse("tasks:mine"))
        self.assertContains(r, "Nothing assigned to you")

    def test_page_denied_without_tasks_view(self):
        nobody = User.objects.create_user("nobody", password="x")
        nobody.primary_role = None
        nobody.save(update_fields=["primary_role"])
        self.client.force_login(nobody)

        r = self.client.get(reverse("tasks:mine"))

        self.assertRedirects(r, reverse("core:dashboard"))


class TaskDetailPageTests(TaskWorkflowBase):
    def test_detail_renders_every_section(self):
        task = self.make_task(reviewer=self.pm, estimated_hours=Decimal("3"))
        TaskChecklistItem.objects.create(task=task, text="Header done")
        TaskComment.objects.create(task=task, author=self.pm, body="Nice work")
        blocker = self.make_task(title="API ready")
        task.blocked_by.add(blocker)
        self.client.login(username="dev", password="x")

        r = self.client.get(task.get_absolute_url())

        self.assertEqual(r.status_code, 200)
        for fragment in ("Checklist", "Header done", "Comments", "Nice work",
                         "Dependencies", "API ready", "Attachments",
                         "Blocked", "Submit for review"):
            self.assertContains(r, fragment)

    def test_reviewer_sees_approve_and_reopen_controls(self):
        task = self.make_task(reviewer=self.pm,
                              approval_status=Task.Approval.SUBMITTED)
        self.client.login(username="pm", password="x")

        r = self.client.get(task.get_absolute_url())

        self.assertContains(r, "Review requested")
        self.assertContains(r, reverse("projects:task_approve", args=[task.pk]))
        self.assertContains(r, reverse("projects:task_reopen", args=[task.pk]))

    def test_assignee_does_not_see_review_controls(self):
        task = self.make_task(reviewer=self.pm,
                              approval_status=Task.Approval.SUBMITTED)
        self.client.login(username="dev", password="x")

        r = self.client.get(task.get_absolute_url())

        self.assertNotContains(r, reverse("projects:task_approve", args=[task.pk]))
        self.assertContains(r, "Waiting on")

    def test_assign_controls_hidden_without_assign_perm(self):
        task = self.make_task()
        self.client.login(username="dev", password="x")
        r = self.client.get(task.get_absolute_url())
        self.assertNotContains(r, reverse("projects:task_assign", args=[task.pk]))

    def test_renders_when_related_users_are_gone(self):
        """Author/uploader/reviewer are all SET_NULL — a deleted colleague must
        not take the page down (`|default:` evaluates its argument eagerly)."""
        task = self.make_task(assignee=None,
                              approval_status=Task.Approval.SUBMITTED)
        TaskComment.objects.create(task=task, author=None, body="Orphaned note")
        TaskAttachment.objects.create(
            task=task, uploaded_by=None,
            file=SimpleUploadedFile("orphan.txt", b"x", "text/plain"))
        self.client.login(username="dev", password="x")

        r = self.client.get(task.get_absolute_url())

        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Orphaned note")
        self.assertContains(r, "System")     # the null-author fallback

    def test_card_comment_block_is_not_rendered_as_text(self):
        """The card's docs used to be a wrapped {# #}, which leaked onto the
        board as visible text."""
        self.make_task(title="Visible task")
        self.client.login(username="dev", password="x")

        r = self.client.get(self.project.get_absolute_url())

        self.assertContains(r, "Visible task")
        self.assertNotContains(r, "Expects: t (Task)")


class BoardTests(TaskWorkflowBase):
    def test_project_board_has_four_columns_with_counts(self):
        self.make_task(title="Queued")
        self.make_task(title="Running", status=Task.Status.IN_PROGRESS)
        self.make_task(title="In review", reviewer=self.pm,
                       status=Task.Status.IN_PROGRESS,
                       approval_status=Task.Approval.SUBMITTED)
        self.make_task(title="Finished", status=Task.Status.DONE)
        self.client.login(username="dev", password="x")

        r = self.client.get(self.project.get_absolute_url())

        self.assertEqual(r.context["task_summary"],
                         {"todo": 1, "in_progress": 1, "submitted": 1,
                          "done": 1, "total": 4})
        for label in ("To do", "In progress", "Submitted", "Done"):
            self.assertContains(r, label)

    def test_card_shows_checklist_progress_and_blocked_flag(self):
        blocker = self.make_task(title="API ready")
        task = self.make_task(title="Wire up UI")
        task.blocked_by.add(blocker)
        TaskChecklistItem.objects.create(task=task, text="a", is_done=True)
        TaskChecklistItem.objects.create(task=task, text="b")
        self.client.login(username="dev", password="x")

        r = self.client.get(self.project.get_absolute_url())

        self.assertContains(r, "1/2")
        self.assertContains(r, "Blocked by 1")
