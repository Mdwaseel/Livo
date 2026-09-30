"""Client approvals.

Written from the outside in, like the calendar's public-link tests: the client
side is exercised by URL with no login, and every "can't" is asserted as a
response code or an unchanged row — never as something a template hides.
"""
import io
import re
import shutil
import tempfile
from datetime import date, timedelta

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from calendar_hub.tests.factories import make_user
from clients.models import Client, Contact
from core.models import Notification
from projects.models import Project

from . import approvals
from .models import (ApprovalAsset, ApprovalEvent, ApprovalRequest, ClientActivity,
                     ClientReviewer)
from .testing import ReviewClient

MEDIA = tempfile.mkdtemp(prefix="approvals-test-")
FUTURE = timezone.localdate() + timedelta(days=10)


def png_bytes():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (15, 138, 128)).save(buffer, "PNG")
    return buffer.getvalue()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   MEDIA_ROOT=MEDIA)
class ApprovalTestCase(TestCase):
    client_class = ReviewClient

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.manager = make_user("mgr", role="Manager")
        self.hr = make_user("hr", role="HR")
        self.acme = Client.objects.create(name="Acme Foods")
        self.priya = Contact.objects.create(client=self.acme, name="Priya Shah",
                                            email="Priya@Acme.com", is_primary=True)
        self.social = Project.objects.create(client=self.acme, name="Social media",
                                             status=Project.Status.ACTIVE)
        self.blog = ClientActivity.objects.create(
            project=self.social, kind="POST", platform="WEBSITE", date=FUTURE,
            title="Diwali offer blog", details="Draft caption", created_by=self.manager)

        self.globex = Client.objects.create(name="Globex")
        self.rival = Project.objects.create(client=self.globex, name="Globex social",
                                            status=Project.Status.ACTIVE)
        self.rival_post = ClientActivity.objects.create(
            project=self.rival, kind="POST", date=FUTURE, title="Globex secret launch")

    # --- helpers ---

    def send_request(self, **extra):
        self.client.force_login(self.manager)
        data = {"people": [f"c{self.priya.pk}"], "content": "Celebrate Diwali with 20% off",
                "message": "Please check the dates", "links": "Draft | https://docs.google.com/d/1"}
        data.update(extra)
        response = self.client.post(
            reverse("client_calendar:approval_create", args=[self.blog.pk]), data)
        self.client.logout()
        return response

    def reviewer(self, email="priya@acme.com"):
        return ClientReviewer.objects.get(email=email)

    def verify(self, reviewer):
        """Go through the emailed-code step as the reviewer would."""
        base = reviewer.get_absolute_url()
        self.client.post(reverse("client_review:send_code", args=[reviewer.token]),
                         {"next": base})
        code = re.match(r"(\d{6})", mail.outbox[-1].subject).group(1)
        return self.client.post(reverse("client_review:check_code", args=[reviewer.token]),
                                {"next": base, "code": code})


class SendingTests(ApprovalTestCase):
    def test_team_sends_request_and_client_is_emailed_a_private_link(self):
        upload = SimpleUploadedFile("hero.png", png_bytes(), content_type="image/png")
        response = self.send_request(files=[upload])

        approval = ApprovalRequest.objects.get()
        self.assertRedirects(response, approval.get_absolute_url(),
                             fetch_redirect_response=False)
        self.assertEqual(approval.status, ApprovalRequest.Status.PENDING)
        self.assertEqual(approval.version, 1)
        self.assertEqual(approval.assets.count(), 2)  # the image and the link

        self.blog.refresh_from_db()
        self.assertEqual(self.blog.status, ClientActivity.Status.APPROVAL)
        self.assertEqual(approval.status_before, ClientActivity.Status.PLANNED)

        reviewer = self.reviewer()
        self.assertEqual(reviewer.contact, self.priya)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["priya@acme.com"])
        self.assertIn(reviewer.review_url(approval), message.body)
        self.assertEqual(message.reply_to, [self.manager.email])
        self.assertTrue(ApprovalEvent.objects.filter(request=approval, kind="SENT").exists())

    def test_nothing_to_review_or_nobody_to_send_to_is_refused(self):
        response = self.send_request(people=[], content="", links="")
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Choose at least one person", status_code=400)
        self.assertContains(response, "Add the copy, a file or a link", status_code=400)
        self.assertFalse(ApprovalRequest.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_someone_new_can_be_added_by_email(self):
        self.send_request(people=[], new_name=["Rahul Brand"], new_email=["rahul@acme.com"])
        approval = ApprovalRequest.objects.get()
        self.assertEqual([r.email for r in approval.reviewers.all()], ["rahul@acme.com"])

    def test_bad_link_is_explained(self):
        response = self.send_request(links="not a link")
        self.assertContains(response, "a full link", status_code=400)

    def test_people_without_project_edit_cannot_send(self):
        self.client.force_login(self.hr)
        self.client.post(reverse("client_calendar:approval_create", args=[self.blog.pk]),
                         {"people": [f"c{self.priya.pk}"], "content": "x"})
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_new_version_replaces_the_waiting_one_and_can_keep_its_files(self):
        self.send_request(files=[SimpleUploadedFile("hero.png", png_bytes(), content_type="image/png")])
        first = ApprovalRequest.objects.get()
        image = first.assets.get(url="")

        self.send_request(carry=[str(image.pk)], content="Updated copy")
        first.refresh_from_db()
        second = ApprovalRequest.objects.exclude(pk=first.pk).get()
        self.assertEqual(first.status, ApprovalRequest.Status.WITHDRAWN)
        self.assertEqual(second.version, 2)
        self.assertEqual(second.status_before, ClientActivity.Status.PLANNED)
        carried = second.assets.get(url="")
        self.assertEqual(carried.file.name, image.file.name)

    def test_withdraw_puts_the_calendar_back(self):
        self.blog.status = ClientActivity.Status.IN_PROGRESS
        self.blog.save()
        self.send_request()
        approval = ApprovalRequest.objects.get()
        self.client.force_login(self.manager)
        self.client.post(reverse("client_calendar:approval_withdraw", args=[approval.pk]))
        approval.refresh_from_db()
        self.blog.refresh_from_db()
        self.assertEqual(approval.status, ApprovalRequest.Status.WITHDRAWN)
        self.assertEqual(self.blog.status, ClientActivity.Status.IN_PROGRESS)

    def test_team_pages_render(self):
        self.send_request()
        approval = ApprovalRequest.objects.get()
        self.client.force_login(self.manager)
        for url in (reverse("client_calendar:approvals"),
                    reverse("client_calendar:approvals") + "?tab=all",
                    approval.get_absolute_url(),
                    reverse("client_calendar:approval_create", args=[self.blog.pk]),
                    reverse("client_calendar:planner", args=[self.acme.pk]) + f"?date={FUTURE:%Y-%m}",
                    reverse("client_calendar:activity_edit", args=[self.blog.pk]),
                    reverse("client_calendar:index")):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        planner = self.client.get(reverse("client_calendar:planner", args=[self.acme.pk])
                                  + f"?date={FUTURE:%Y-%m}")
        self.assertContains(planner, "Waiting on client")

    def test_remind_emails_again(self):
        self.send_request()
        approval = ApprovalRequest.objects.get()
        self.client.force_login(self.manager)
        self.client.post(reverse("client_calendar:approval_remind", args=[approval.pk]))
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("Reminder", mail.outbox[1].subject)
        self.assertEqual(approval.recipients.get().email_count, 2)


class ReviewerAccessTests(ApprovalTestCase):
    def setUp(self):
        super().setUp()
        self.send_request()
        self.approval = ApprovalRequest.objects.get()
        self.rv = self.reviewer()
        self.page = self.rv.review_url(self.approval)

    def test_the_link_alone_shows_nothing_but_a_code_prompt(self):
        response = self.client.get(self.page)
        self.assertContains(response, "Confirm it")
        self.assertContains(response, "pr•••@acme.com")
        self.assertNotContains(response, "Diwali offer blog")
        self.assertNotContains(response, "Celebrate Diwali")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow, noarchive")
        # Not no-referrer: that makes browsers post `Origin: null`, and every
        # form on these pages would fail Django's CSRF check (found in a real
        # browser; the test client never sends Origin).
        self.assertEqual(response["Referrer-Policy"], "same-origin")

    def test_signed_in_teammate_is_not_let_through_either(self):
        self.client.force_login(self.manager)
        self.assertNotContains(self.client.get(self.page), "Celebrate Diwali")

    def test_wrong_code_does_not_open_it(self):
        self.client.post(reverse("client_review:send_code", args=[self.rv.token]),
                         {"next": self.page})
        response = self.client.post(reverse("client_review:check_code", args=[self.rv.token]),
                                    {"next": self.page, "code": "000000"
                                     if not mail.outbox[-1].subject.startswith("000000") else "111111"})
        self.assertIn("v=wrong", response["Location"])
        self.assertNotContains(self.client.get(self.page), "Celebrate Diwali")

    def test_right_code_opens_it_and_records_one_open(self):
        response = self.verify(self.rv)
        self.assertEqual(response["Location"], self.rv.get_absolute_url())
        page = self.client.get(self.page)
        self.assertContains(page, "Celebrate Diwali")
        self.assertContains(page, "Please check the dates")
        self.client.get(self.page)
        self.assertEqual(self.approval.events.filter(kind="OPENED").count(), 1)
        self.assertIsNotNone(self.approval.recipients.get().first_opened_at)

    def test_code_is_single_use_and_guesses_run_out(self):
        self.client.post(reverse("client_review:send_code", args=[self.rv.token]),
                         {"next": self.page})
        code = mail.outbox[-1].subject[:6]
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(6):
            self.client.post(reverse("client_review:check_code", args=[self.rv.token]),
                             {"next": self.page, "code": wrong})
        response = self.client.post(reverse("client_review:check_code", args=[self.rv.token]),
                                    {"next": self.page, "code": code})
        self.assertNotEqual(response["Location"], self.page)
        self.assertNotContains(self.client.get(self.page), "Celebrate Diwali")

    def test_next_cannot_point_off_site(self):
        self.client.post(reverse("client_review:send_code", args=[self.rv.token]),
                         {"next": self.page})
        code = mail.outbox[-1].subject[:6]
        response = self.client.post(reverse("client_review:check_code", args=[self.rv.token]),
                                    {"next": "https://evil.example/r/", "code": code})
        self.assertEqual(response["Location"], self.rv.get_absolute_url())

    def test_one_reviewers_browser_does_not_open_another_reviewers_page(self):
        self.verify(self.rv)
        self.send_request(people=[], new_name=["Rahul"], new_email=["rahul@acme.com"])
        rahul = self.reviewer("rahul@acme.com")
        latest = ApprovalRequest.objects.order_by("-pk").first()
        response = self.client.get(rahul.review_url(latest))
        self.assertContains(response, "Confirm it")

    def test_another_clients_request_is_a_404_even_when_verified(self):
        self.verify(self.rv)
        other = approvals.create_request(
            self.rival_post, reviewers=[approvals.reviewer_for(self.globex, name="G", email="g@globex.com")],
            content="Globex secret", actor=self.manager)
        response = self.client.get(reverse("client_review:detail", args=[self.rv.token, other.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "Globex secret", status_code=404)

    def test_unknown_and_revoked_links(self):
        self.assertEqual(self.client.get(reverse("client_review:inbox", args=["nope" * 8])).status_code, 404)
        self.verify(self.rv)
        self.client.force_login(self.manager)
        self.client.post(reverse("client_calendar:reviewer_access", args=[self.rv.pk]),
                         {"action": "revoke", "next": self.approval.get_absolute_url()})
        self.client.logout()
        self.assertEqual(self.client.get(self.page).status_code, 410)

    def test_signing_out_devices_needs_a_new_code(self):
        self.verify(self.rv)
        self.assertContains(self.client.get(self.page), "Celebrate Diwali")
        self.rv.refresh_from_db()
        self.rv.sign_out_devices()
        self.assertContains(self.client.get(self.page), "Confirm it")

    def test_inbox_lists_what_is_waiting(self):
        self.verify(self.rv)
        response = self.client.get(self.rv.get_absolute_url())
        self.assertContains(response, "Hi Priya")
        self.assertContains(response, "Diwali offer blog")


class DecisionTests(ApprovalTestCase):
    def setUp(self):
        super().setUp()
        self.send_request()
        self.approval = ApprovalRequest.objects.get()
        self.rv = self.reviewer()
        self.verify(self.rv)
        self.decide_url = reverse("client_review:decide", args=[self.rv.token, self.approval.pk])
        mail.outbox.clear()

    def test_approve_updates_everything_and_tells_the_team(self):
        response = self.client.post(self.decide_url, {"decision": "approve", "feedback": "Looks great"})
        self.assertIn("done=approved", response["Location"])
        self.approval.refresh_from_db()
        self.blog.refresh_from_db()
        self.assertEqual(self.approval.status, ApprovalRequest.Status.APPROVED)
        self.assertEqual(self.approval.decided_by, self.rv)
        self.assertEqual(self.approval.feedback, "Looks great")
        self.assertEqual(self.blog.status, ClientActivity.Status.APPROVED)
        self.assertTrue(Notification.objects.filter(user=self.manager, text__contains="approved").exists())
        self.assertEqual([m.to for m in mail.outbox], [[self.manager.email]])
        self.assertEqual(mail.outbox[0].reply_to, ["priya@acme.com"])

    def test_a_second_answer_is_not_recorded(self):
        self.client.post(self.decide_url, {"decision": "approve"})
        response = self.client.post(self.decide_url, {"decision": "changes", "feedback": "wait"})
        self.assertIn("done=late", response["Location"])
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, ApprovalRequest.Status.APPROVED)

    def test_changes_need_feedback(self):
        response = self.client.post(self.decide_url, {"decision": "changes", "feedback": "  "})
        self.assertIn("needs-feedback", response["Location"])
        self.approval.refresh_from_db()
        self.assertTrue(self.approval.is_pending)

    def test_changes_requested_moves_the_activity_back_to_in_progress(self):
        self.client.post(self.decide_url, {"decision": "changes", "feedback": "Use the red logo"})
        self.approval.refresh_from_db()
        self.blog.refresh_from_db()
        self.assertEqual(self.approval.status, ApprovalRequest.Status.CHANGES)
        self.assertEqual(self.blog.status, ClientActivity.Status.IN_PROGRESS)
        page = self.client.get(self.rv.review_url(self.approval))
        self.assertContains(page, "Use the red logo")

    def test_a_status_set_by_hand_is_not_overwritten(self):
        self.blog.status = ClientActivity.Status.DONE
        self.blog.save()
        self.client.post(self.decide_url, {"decision": "approve"})
        self.blog.refresh_from_db()
        self.assertEqual(self.blog.status, ClientActivity.Status.DONE)

    def test_unverified_browser_cannot_decide(self):
        self.client.cookies.clear()
        self.client.post(self.decide_url, {"decision": "approve"})
        self.approval.refresh_from_db()
        self.assertTrue(self.approval.is_pending)


class FileTests(ApprovalTestCase):
    def setUp(self):
        super().setUp()
        self.send_request(files=[
            SimpleUploadedFile("hero.png", png_bytes(), content_type="image/png"),
            SimpleUploadedFile("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                               content_type="image/svg+xml"),
        ])
        self.approval = ApprovalRequest.objects.get()
        self.rv = self.reviewer()
        self.image = self.approval.assets.get(file__endswith=".png")
        self.svg = self.approval.assets.get(file__endswith=".svg")

    def url(self, asset):
        return reverse("client_review:asset", args=[self.rv.token, self.approval.pk, asset.pk])

    def test_files_need_a_verified_browser(self):
        self.assertEqual(self.client.get(self.url(self.image)).status_code, 404)

    def test_images_inline_scripts_never(self):
        self.verify(self.rv)
        image = self.client.get(self.url(self.image))
        self.assertEqual(image.status_code, 200)
        self.assertTrue(image["Content-Disposition"].startswith("inline"))
        svg = self.client.get(self.url(self.svg))
        self.assertTrue(svg["Content-Disposition"].startswith("attachment"))
        self.assertEqual(svg["Content-Security-Policy"], "sandbox")
        self.assertEqual(svg["X-Content-Type-Options"], "nosniff")

    def test_byte_ranges_for_players(self):
        self.verify(self.rv)
        response = self.client.get(self.url(self.image), HTTP_RANGE="bytes=0-3")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b"".join(response.streaming_content), self.image.file.open("rb").read()[:4])
        self.assertTrue(response["Content-Range"].startswith("bytes 0-3/"))

    def test_deleting_the_activity_removes_its_files(self):
        storage, name = self.image.file.storage, self.image.file.name
        self.assertTrue(storage.exists(name))
        self.client.force_login(self.manager)
        self.client.post(reverse("client_calendar:activity_delete", args=[self.blog.pk]))
        self.assertFalse(storage.exists(name))
        self.assertFalse(ApprovalAsset.objects.exists())
