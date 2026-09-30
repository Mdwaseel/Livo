from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.models import Client
from finance.models import Payment
from .models import DEFAULT_ONBOARDING, OnboardingItem, Project

User = get_user_model()


class OnboardingSeedTests(TestCase):
    def test_new_project_gets_default_checklist(self):
        client = Client.objects.create(name="Seed Co")
        project = Project.objects.create(client=client, name="Site")
        keys = list(project.onboarding_items.values_list("key", flat=True))
        self.assertEqual(keys, [k for k, _, _ in DEFAULT_ONBOARDING])
        auto = project.onboarding_items.get(key="advance_received")
        self.assertTrue(auto.is_auto)
        self.assertFalse(auto.is_done)

    def test_seed_is_idempotent(self):
        client = Client.objects.create(name="Seed Co")
        project = Project.objects.create(client=client, name="Site")
        project.ensure_onboarding()
        project.save()  # non-create save must not duplicate either
        self.assertEqual(project.onboarding_items.count(), len(DEFAULT_ONBOARDING))

    def test_progress_helper(self):
        client = Client.objects.create(name="Seed Co")
        project = Project.objects.create(client=client, name="Site")
        self.assertEqual(project.onboarding_progress,
                         {"done": 0, "total": 6, "percent": 0})
        project.onboarding_items.filter(key="contract_signed").update(is_done=True)
        self.assertEqual(project.onboarding_progress["done"], 1)
        self.assertEqual(project.onboarding_progress["percent"], 17)


class OnboardingAutoTickTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(name="Auto Co")
        self.project = Project.objects.create(client=self.client_obj, name="Site")

    def _item(self):
        return self.project.onboarding_items.get(key="advance_received")

    def test_advance_payment_auto_ticks(self):
        Payment.objects.create(project=self.project, amount=Decimal("5000"),
                               payment_type=Payment.Type.ADVANCE,
                               received_on=date.today())
        item = self._item()
        self.assertTrue(item.is_done)
        self.assertEqual(item.completed_on, date.today())

    def test_any_payment_counts_as_advance(self):
        Payment.objects.create(project=self.project, amount=Decimal("5000"),
                               payment_type=Payment.Type.MILESTONE,
                               received_on=date.today())
        self.assertTrue(self._item().is_done)

    def test_deleting_all_payments_unticks(self):
        pay = Payment.objects.create(project=self.project, amount=Decimal("5000"),
                                     payment_type=Payment.Type.ADVANCE,
                                     received_on=date.today())
        pay.delete()
        item = self._item()
        self.assertFalse(item.is_done)
        self.assertIsNone(item.completed_on)


class OnboardingToggleViewTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(name="Toggle Co")
        self.project = Project.objects.create(client=self.client_obj, name="Site")
        self.pm = User.objects.create_user("pm", password="x", role="PM")
        self.dev = User.objects.create_user("dev", password="x", role="DEV")
        # Membership is what makes a project visible to a non-manager now.
        self.project.members.add(self.pm, self.dev)

    def _toggle(self, item):
        return self.client.post(
            reverse("projects:onboarding_toggle", args=[item.pk])
        )

    def test_pm_can_toggle_manual_item(self):
        self.client.login(username="pm", password="x")
        item = self.project.onboarding_items.get(key="contract_signed")
        r = self._toggle(item)
        self.assertRedirects(r, self.project.get_absolute_url())
        item.refresh_from_db()
        self.assertTrue(item.is_done)
        self.assertEqual(item.completed_by, self.pm)
        self.assertEqual(item.completed_on, date.today())
        # toggling back clears the stamps
        self._toggle(item)
        item.refresh_from_db()
        self.assertFalse(item.is_done)
        self.assertIsNone(item.completed_on)
        self.assertIsNone(item.completed_by)

    def test_auto_item_cannot_be_toggled(self):
        self.client.login(username="pm", password="x")
        item = self.project.onboarding_items.get(key="advance_received")
        self._toggle(item)
        item.refresh_from_db()
        self.assertFalse(item.is_done)

    def test_developer_role_is_denied(self):
        self.client.login(username="dev", password="x")
        item = self.project.onboarding_items.get(key="contract_signed")
        self._toggle(item)
        item.refresh_from_db()
        self.assertFalse(item.is_done)

    def test_toggles_hidden_for_non_writers(self):
        self.client.login(username="dev", password="x")
        r = self.client.get(self.project.get_absolute_url())
        self.assertContains(r, "Onboarding")
        self.assertNotContains(r, "onboarding_toggle")


class DeliveryWorkspaceTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(name="Delivery Co")
        self.project = Project.objects.create(client=self.client_obj, name="Site")
        self.dev = User.objects.create_user("dev2", password="x", role="DEV")
        self.sales = User.objects.create_user("sales", password="x", role="SALES")
        self.project.members.add(self.dev, self.sales)

    def test_dev_can_add_task_and_advance_status(self):
        self.client.login(username="dev2", password="x")
        r = self.client.post(
            reverse("projects:task_create", args=[self.project.pk]),
            {"title": "Build homepage", "priority": "HIGH"},
        )
        self.assertRedirects(r, self.project.get_absolute_url())
        task = self.project.tasks.get()
        self.assertEqual(task.priority, "HIGH")
        self.client.post(reverse("projects:task_set_status", args=[task.pk]),
                         {"status": "DONE"})
        task.refresh_from_db()
        self.assertEqual(task.status, "DONE")
        self.assertIsNotNone(task.completed_on)
        self.assertEqual(
            self.project.task_summary,
            {"todo": 0, "in_progress": 0, "submitted": 0, "done": 1, "total": 1})

    def test_sales_cannot_write_delivery(self):
        self.client.login(username="sales", password="x")
        self.client.post(reverse("projects:task_create", args=[self.project.pk]),
                         {"title": "Nope"})
        self.assertEqual(self.project.tasks.count(), 0)
        r = self.client.get(self.project.get_absolute_url())
        self.assertNotContains(r, "task_create")
        self.assertNotContains(r, 'Edit project')

    def test_worklog_create_and_this_month(self):
        from django.utils import timezone
        self.client.login(username="dev2", password="x")
        today = timezone.localdate()
        r = self.client.post(
            reverse("projects:worklog_create", args=[self.project.pk]),
            {"date": today.isoformat(), "category": "DEV",
             "description": "Shipped header", "hours": "2.5"},
        )
        self.assertRedirects(r, self.project.get_absolute_url())
        self.project.work_logs.create(date=today.replace(year=today.year - 1),
                                      description="old", category="DEV")
        this_month = self.project.worklog_this_month()
        self.assertEqual(this_month.count(), 1)
        self.assertEqual(this_month.first().description, "Shipped header")

    def test_asset_upload_image_renders_thumbnail(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        # 1x1 px GIF
        gif = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
               b"\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
               b"\x00\x02\x02D\x01\x00;")
        self.client.login(username="dev2", password="x")
        r = self.client.post(
            reverse("projects:asset_upload", args=[self.project.pk]),
            {"file": SimpleUploadedFile("proof.gif", gif, "image/gif"),
             "title": "Homepage proof", "category": "PROOF", "is_report_proof": "1"},
        )
        self.assertRedirects(r, self.project.get_absolute_url())
        asset = self.project.assets.get()
        self.assertTrue(asset.is_image)
        self.assertTrue(asset.is_report_proof)
        body = self.client.get(self.project.get_absolute_url()).content.decode()
        self.assertIn(f'<img src="{asset.file.url}"', body)
        asset.file.delete(save=False)

    def test_milestone_add_and_mark_done(self):
        self.client.login(username="dev2", password="x")
        self.client.post(reverse("projects:milestone_create", args=[self.project.pk]),
                         {"title": "Design sign-off"})
        m = self.project.milestones.get()
        self.client.post(reverse("projects:milestone_set_status", args=[m.pk]),
                         {"status": "DONE"})
        m.refresh_from_db()
        self.assertEqual(m.status, "DONE")

    def test_project_edit_saves_links(self):
        self.client.login(username="dev2", password="x")
        r = self.client.post(reverse("projects:edit", args=[self.project.pk]), {
            "name": "Site", "status": "ACTIVE", "live_url": "https://example.com",
            "staging_url": "", "repo_url": "https://github.com/x/y", "assets_url": "",
        })
        self.assertRedirects(r, self.project.get_absolute_url())
        self.project.refresh_from_db()
        self.assertEqual(self.project.live_url, "https://example.com")
        self.assertEqual(self.project.status, "ACTIVE")
