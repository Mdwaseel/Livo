"""Regressions for the delivery/finance bugs found in the July 2026 audit."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ai_engine.services import build_prompt
from clients.models import Client
from documents.models import DocumentType
from finance.models import Payment

from .models import Milestone, RecurringTask, Task, WorkLogEntry

User = get_user_model()


class TaskReviewStateTests(TestCase):
    """Moving an approved task back to TODO cleared only a pending submission,
    so approved_by / approved_on survived onto live work."""

    def setUp(self):
        self.owner = User.objects.create_user("owner", password="x", role="OWNER")
        self.reviewer = User.objects.create_user("rev", password="x", role="PM")
        self.client_obj = Client.objects.create(name="Taskco")
        self.project = self.client_obj.projects.first()
        self.task = Task.objects.create(
            project=self.project, title="Ship it", assignee=self.owner,
            reviewer=self.reviewer, status=Task.Status.DONE,
            approval_status=Task.Approval.APPROVED, approved_by=self.reviewer,
            approved_on="2026-07-01T10:00:00Z", completed_on=date(2026, 7, 1))
        self.client.force_login(self.owner)

    def test_reopening_to_todo_drops_the_sign_off(self):
        self.client.post(reverse("projects:task_set_status", args=[self.task.pk]),
                         {"status": Task.Status.TODO})

        self.task.refresh_from_db()
        self.assertEqual(self.task.approval_status, Task.Approval.NONE)
        self.assertIsNone(self.task.approved_by)
        self.assertIsNone(self.task.approved_on)
        self.assertIsNone(self.task.completed_on)

    def test_reopened_state_is_preserved_not_overwritten(self):
        self.task.approval_status = Task.Approval.REOPENED
        self.task.save(update_fields=["approval_status"])

        self.client.post(reverse("projects:task_set_status", args=[self.task.pk]),
                         {"status": Task.Status.IN_PROGRESS})

        self.task.refresh_from_db()
        self.assertEqual(self.task.approval_status, Task.Approval.REOPENED)

    def test_the_edit_form_clears_it_too(self):
        self.client.post(reverse("projects:task_edit", args=[self.task.pk]),
                         {"title": "Ship it", "status": Task.Status.IN_PROGRESS})

        self.task.refresh_from_db()
        self.assertIsNone(self.task.approved_by)


class TaskEstimateTests(TestCase):
    """_decimal_or_none returns None for 'blank' AND for 'unreadable', and the
    result was assigned unconditionally — so a typo wiped the estimate."""

    def setUp(self):
        self.owner = User.objects.create_user("owner", password="x", role="OWNER")
        self.project = Client.objects.create(name="Estco").projects.first()
        self.task = Task.objects.create(project=self.project, title="T",
                                        estimated_hours=Decimal("8"))
        self.client.force_login(self.owner)

    def _edit(self, **extra):
        return self.client.post(reverse("projects:task_edit", args=[self.task.pk]),
                                {"title": "T", **extra})

    def test_unreadable_hours_leave_the_estimate_alone(self):
        self._edit(estimated_hours="eight-ish")

        self.task.refresh_from_db()
        self.assertEqual(self.task.estimated_hours, Decimal("8"))

    def test_a_blank_field_still_clears_it(self):
        self._edit(estimated_hours="")

        self.task.refresh_from_db()
        self.assertIsNone(self.task.estimated_hours)

    def test_a_valid_value_is_stored(self):
        self._edit(estimated_hours="12.5")

        self.task.refresh_from_db()
        self.assertEqual(self.task.estimated_hours, Decimal("12.5"))


class MilestoneCompletionDateTests(TestCase):
    """Monthly reports filtered on updated_at, so editing an old milestone made
    it resurface as 'reached this month'."""

    def setUp(self):
        self.project = Client.objects.create(name="Mileco").projects.first()

    def test_marking_done_stamps_a_completion_date(self):
        m = Milestone.objects.create(project=self.project, title="Launch")

        m.save(update_fields=m.apply_status(Milestone.Status.DONE))

        m.refresh_from_db()
        self.assertIsNotNone(m.completed_on)

    def test_a_direct_save_is_stamped_too(self):
        # Fixtures, the admin and bulk imports never call apply_status.
        m = Milestone.objects.create(project=self.project, title="Direct",
                                     status=Milestone.Status.DONE)

        m.refresh_from_db()
        self.assertIsNotNone(m.completed_on)

    def test_reopening_clears_the_date(self):
        m = Milestone.objects.create(project=self.project, title="Launch",
                                     status=Milestone.Status.DONE)

        m.save(update_fields=m.apply_status(Milestone.Status.IN_PROGRESS))

        m.refresh_from_db()
        self.assertIsNone(m.completed_on)

    def test_editing_a_done_milestone_does_not_move_its_date(self):
        m = Milestone.objects.create(project=self.project, title="Launch",
                                     status=Milestone.Status.DONE)
        original = m.completed_on

        m.title = "Launch (v2)"
        m.save()

        m.refresh_from_db()
        self.assertEqual(m.completed_on, original)


class RecurringMonthlyAnchorTests(TestCase):
    """A monthly template anchored on the 31st clamped to Feb 28 and then kept
    stepping from 28 — the anchor day was lost after the first short month."""

    def test_a_month_end_template_returns_to_the_anchor_day(self):
        project = Client.objects.create(name="Recurco").projects.first()
        template = RecurringTask.objects.create(
            project=project, title="Month-end report",
            frequency=RecurringTask.Frequency.MONTHLY, next_run=date(2026, 1, 31))

        run = date(2026, 1, 31)
        schedule = []
        for _ in range(4):
            run = template._step(run)
            schedule.append(run)

        self.assertEqual(schedule, [date(2026, 2, 28), date(2026, 3, 31),
                                    date(2026, 4, 30), date(2026, 5, 31)])

    def test_the_anchor_is_captured_on_save(self):
        project = Client.objects.create(name="Anchorco").projects.first()

        template = RecurringTask.objects.create(
            project=project, title="T", frequency=RecurringTask.Frequency.MONTHLY,
            next_run=date(2026, 3, 17))

        self.assertEqual(template.anchor_day, 17)


class PaymentValidationTests(TestCase):
    """Amounts and dates went to the DB raw (a 500 on anything malformed) and
    the choice fields were never checked against their enums."""

    def setUp(self):
        self.owner = User.objects.create_user("owner", password="x", role="OWNER")
        self.project = Client.objects.create(name="Payco").projects.first()
        self.client.force_login(self.owner)
        self.url = reverse("finance:payment_create", args=[self.project.pk])

    def _post(self, **overrides):
        data = {"amount": "1000", "received_on": "2026-07-01",
                "payment_type": Payment.Type.ADVANCE, "method": Payment.Method.UPI}
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_a_valid_payment_is_recorded(self):
        self._post()

        self.assertEqual(Payment.objects.count(), 1)

    def test_an_unparseable_amount_is_rejected_not_crashed(self):
        r = self._post(amount="one thousand")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(Payment.objects.count(), 0)

    def test_a_missing_amount_is_rejected_not_crashed(self):
        r = self.client.post(self.url, {"received_on": "2026-07-01"})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(Payment.objects.count(), 0)

    def test_a_malformed_date_is_rejected(self):
        self._post(received_on="01/07/2026")

        self.assertEqual(Payment.objects.count(), 0)

    def test_a_negative_amount_is_rejected(self):
        self._post(amount="-500")

        self.assertEqual(Payment.objects.count(), 0)

    def test_junk_choices_fall_back_to_valid_ones(self):
        self._post(payment_type="GARBAGE", method="NOPE")

        payment = Payment.objects.get()
        self.assertIn(payment.payment_type, Payment.Type.values)
        self.assertIn(payment.method, Payment.Method.values)

    def test_an_invoice_from_another_project_is_refused(self):
        other = Client.objects.create(name="Otherco").projects.first()
        foreign = other.documents.create(
            title="Their invoice", document_type=DocumentType.objects.create(
                name="Invoice", slug="invoice"))

        self._post(invoice=foreign.pk)

        self.assertEqual(Payment.objects.count(), 0)


class WorklogTotalTests(TestCase):
    """The project page summed the capped row list, so the filtered total
    under-reported once a project passed the cap."""

    def test_the_total_covers_every_matching_entry_not_just_the_page(self):
        from . import views

        owner = User.objects.create_user("owner", password="x", role="OWNER")
        project = Client.objects.create(name="Logco").projects.first()
        entries = [WorkLogEntry(project=project, date=date(2026, 7, 1),
                                description=f"work {i}", hours=Decimal("1"),
                                logged_by=owner)
                   for i in range(views.WORKLOG_PAGE_SIZE + 5)]
        WorkLogEntry.objects.bulk_create(entries)

        self.client.force_login(owner)
        r = self.client.get(reverse("projects:detail", args=[project.pk]))

        expected = Decimal(views.WORKLOG_PAGE_SIZE + 5)
        self.assertEqual(r.context["worklog_filtered_total"], expected)
        self.assertEqual(len(r.context["work_logs"]), views.WORKLOG_PAGE_SIZE)


class AiPromptBraceTests(TestCase):
    """str.format_map parses the WHOLE string, so one literal brace in an
    admin-authored ai_prompt raised ValueError and 500'd the request."""

    class _Type:
        name = "Proposal"

        def __init__(self, prompt):
            self.ai_prompt = prompt

    class _Client:
        name = "Acme"
        city = "Pune"
        gstin = ""

    class _Project:
        name = "Site"
        project_type = "Web"

    def _build(self, prompt):
        return build_prompt(self._Type(prompt), {}, self._Client(), self._Project())

    def test_literal_braces_survive_untouched(self):
        out = self._build("Use CSS like { color: red } here.")

        self.assertIn("{ color: red }", out)

    def test_known_placeholders_are_still_filled(self):
        self.assertIn("Acme", self._build("For {client_name}."))

    def test_unknown_placeholders_are_left_intact(self):
        self.assertIn("{mystery}", self._build("Keep {mystery} as-is."))

    def test_json_examples_do_not_crash(self):
        out = self._build('Return {"status": "ok"} for {project_name}.')

        self.assertIn('{"status": "ok"}', out)
        self.assertIn("Site", out)
