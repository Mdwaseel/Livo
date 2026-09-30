"""The approval pages must work in browsers that break Django's CSRF check.

Clients were seeing "403 Forbidden" on the approval link. Every test here uses
a client with CSRF enforcement on and no CSRF cookie, Origin or Referer — what
Gmail's and Outlook's in-app browsers, older Safari and privacy extensions
often send — and walks the real flow from code to decision.
"""
import time
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import Client as BrowserClient
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from calendar_hub.tests.factories import make_user
from clients.models import Client, Contact
from projects.models import Project

from . import approvals
from .models import ApprovalRequest, ClientActivity, ClientReviewer
from .views_review import form_key


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class StrictBrowserTests(TestCase):
    def setUp(self):
        self.manager = make_user("mgr", role="Manager")
        self.acme = Client.objects.create(name="Acme Foods")
        Contact.objects.create(client=self.acme, name="Priya Shah", email="priya@acme.com")
        project = Project.objects.create(client=self.acme, name="Social media",
                                         status=Project.Status.ACTIVE)
        activity = ClientActivity.objects.create(
            project=project, kind="POST", date=timezone.localdate() + timedelta(days=5),
            title="Diwali blog")
        self.rv = approvals.reviewer_for(self.acme, name="Priya Shah", email="priya@acme.com")
        self.approval = approvals.create_request(activity, reviewers=[self.rv],
                                                 content="Blog copy", actor=self.manager)
        self.page = self.rv.review_url(self.approval)
        self.browser = BrowserClient(enforce_csrf_checks=True)

    def send_code(self, **data):
        return self.browser.post(reverse("client_review:send_code", args=[self.rv.token]),
                                 {"next": self.page, **data})

    def test_whole_flow_works_with_no_csrf_cookie_origin_or_referer(self):
        response = self.send_code(form_key=form_key(self.rv))
        self.assertEqual(response.status_code, 302)
        self.assertIn("v=sent", response["Location"])

        code = mail.outbox[-1].subject[:6]
        response = self.browser.post(
            reverse("client_review:check_code", args=[self.rv.token]),
            {"next": self.page, "code": code, "form_key": form_key(self.rv)})
        self.assertEqual(response["Location"], self.page)

        response = self.browser.post(
            reverse("client_review:decide", args=[self.rv.token, self.approval.pk]),
            {"decision": "approve", "form_key": form_key(self.rv)})
        self.assertIn("done=approved", response["Location"])
        self.approval.refresh_from_db()
        self.assertEqual(self.approval.status, ApprovalRequest.Status.APPROVED)

    def test_pages_carry_the_signed_key_not_a_csrf_token(self):
        response = self.browser.get(self.page)
        self.assertContains(response, 'name="form_key"')
        self.assertNotContains(response, "csrfmiddlewaretoken")
        self.assertNotIn("csrftoken", response.cookies)

    def test_missing_forged_or_someone_elses_key_does_nothing(self):
        other = approvals.reviewer_for(self.acme, name="Rahul", email="rahul@acme.com")
        for data in ({}, {"form_key": "forged"}, {"form_key": form_key(other)}):
            with self.subTest(data=data):
                response = self.send_code(**data)
                self.assertIn("v=stale", response["Location"])
        self.assertEqual(mail.outbox, [])

    def test_a_week_old_page_asks_to_try_again(self):
        key = form_key(self.rv)
        with mock.patch("django.core.signing.time.time", return_value=time.time() + 8 * 86400):
            response = self.send_code(form_key=key)
        self.assertIn("v=stale", response["Location"])
        self.assertContains(self.browser.get(response["Location"]), "open for a while")

    def test_a_decision_needs_the_key_even_from_a_verified_browser(self):
        self.send_code(form_key=form_key(self.rv))
        self.browser.post(reverse("client_review:check_code", args=[self.rv.token]),
                          {"next": self.page, "code": mail.outbox[-1].subject[:6],
                           "form_key": form_key(self.rv)})
        response = self.browser.post(
            reverse("client_review:decide", args=[self.rv.token, self.approval.pk]),
            {"decision": "approve"})
        self.assertIn("done=stale", response["Location"])
        self.approval.refresh_from_db()
        self.assertTrue(self.approval.is_pending)

    def test_email_preferences_form(self):
        url = reverse("client_review:updates", args=[self.rv.token])
        self.browser.post(url, {"frequency": "DAILY"})
        self.rv.refresh_from_db()
        self.assertEqual(self.rv.update_frequency, ClientReviewer.Updates.OFF)
        self.browser.post(url, {"frequency": "DAILY", "form_key": form_key(self.rv)})
        self.rv.refresh_from_db()
        self.assertEqual(self.rv.update_frequency, ClientReviewer.Updates.DAILY)


class FriendlyCsrfFailureTests(TestCase):
    def test_team_forms_that_fail_csrf_get_a_readable_page(self):
        browser = BrowserClient(enforce_csrf_checks=True)
        browser.force_login(make_user("mgr", role="Manager"))
        with self.assertLogs("django.security.csrf", level="WARNING") as logs:
            response = browser.post(reverse("client_calendar:approvals"), {})
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "This page expired", status_code=403)
        self.assertIn("csrf_cookie=no", logs.output[0])
