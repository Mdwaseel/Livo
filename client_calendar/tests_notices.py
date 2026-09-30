"""Telling the client about one activity straight away, and several emails at once."""
from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from calendar_hub.tests.factories import make_user
from clients.models import Client, Contact
from projects.models import Project

from . import updates
from .models import (ApprovalRequest, ClientActivity, ClientCalendarLink, ClientReviewer,
                     ClientUpdate)
from .testing import ReviewClient

FUTURE = timezone.localdate() + timedelta(days=10)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   SITE_URL="https://os.example.com", CLIENT_UPDATES_QUIET_MINUTES=20)
class NoticeTestCase(TestCase):
    client_class = ReviewClient

    def setUp(self):
        self.manager = make_user("mgr", role="Manager")
        self.acme = Client.objects.create(name="Acme Foods")
        self.contact = Contact.objects.create(client=self.acme, name="Priya Shah",
                                              email="priya@acme.com", is_primary=True)
        self.social = Project.objects.create(client=self.acme, name="Social media",
                                             status=Project.Status.ACTIVE)
        self.link = ClientCalendarLink.objects.create(client=self.acme, created_by=self.manager)
        self.reel = ClientActivity.objects.create(
            project=self.social, kind="REEL", platform="INSTAGRAM", date=FUTURE,
            title="Diwali reel", status="PLANNED")
        self.client.force_login(self.manager)

    def edit(self, activity, **extra):
        data = {"project": str(activity.project_id), "kind": activity.kind,
                "platform": activity.platform, "status": activity.status,
                "date": activity.date.isoformat(), "time": "", "title": activity.title,
                "details": activity.details, "link": activity.link, "show_to_client": "on"}
        data.update(extra)
        return self.client.post(reverse("client_calendar:activity_edit", args=[activity.pk]), data)


class ActivityNoticeTests(NoticeTestCase):
    def test_marking_it_posted_emails_everyone_chosen_with_the_link(self):
        response = self.edit(self.reel, status="DONE", link="https://instagram.com/p/abc",
                             notify="on", notify_people=[f"c{self.contact.pk}"],
                             notify_emails="rahul@acme.com, Brand@Acme.com; rahul@acme.com",
                             notify_note="Thanks for the quick approval!")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox),
                         ["brand@acme.com", "priya@acme.com", "rahul@acme.com"])
        message = mail.outbox[0]
        self.assertEqual(message.subject, "Published: Diwali reel")
        self.assertIn("https://instagram.com/p/abc", message.body)
        self.assertIn("Thanks for the quick approval!", message.body)
        self.assertIn("List-Unsubscribe", message.extra_headers)
        self.assertEqual(message.reply_to, [self.manager.email])

        done = ClientUpdate.objects.get(kind="DONE")
        self.assertEqual(done.announced_to.count(), 3)

    def test_people_already_told_do_not_get_it_again_in_their_summary(self):
        priya = updates.person_for(self.acme, name="Priya", email="priya@acme.com", contact=self.contact)
        updates.set_frequency(priya, "SOON", now=timezone.now() - timedelta(hours=1))
        ClientUpdate.objects.all().delete()  # the fixture's own "newly planned" isn't under test
        self.edit(self.reel, status="DONE", link="https://instagram.com/p/abc",
                  notify="on", notify_people=[f"c{self.contact.pk}"])
        mail.outbox.clear()
        ClientUpdate.objects.update(created_at=timezone.now() - timedelta(minutes=30))
        # The change is real and inside her window — it's only held back because
        # she was already told about it.
        self.assertTrue(ClientUpdate.objects.filter(kind="DONE").exists())
        priya.refresh_from_db()
        self.assertEqual(updates.pending(priya), [])
        self.assertEqual(updates.run()["sent"], 0)

    def test_moves_say_where_from_and_to(self):
        self.edit(self.reel, date=(FUTURE + timedelta(days=3)).isoformat(),
                  notify="on", notify_emails="priya@acme.com")
        message = mail.outbox[0]
        self.assertEqual(message.subject, "Rescheduled: Diwali reel")
        self.assertIn(f"was {FUTURE:%a} {FUTURE.day} {FUTURE:%b}", message.body)

    def test_planning_something_new_can_tell_them_too(self):
        self.client.post(reverse("client_calendar:activity_create", args=[self.acme.pk]), {
            "project": str(self.social.pk), "kind": "POST", "platform": "INSTAGRAM",
            "status": "PLANNED", "date": FUTURE.isoformat(), "title": "Launch post",
            "show_to_client": "on", "notify": "on", "notify_emails": "priya@acme.com"})
        self.assertEqual(mail.outbox[0].subject, "Newly planned: Launch post")

    def test_without_the_box_nothing_is_sent_now(self):
        self.edit(self.reel, status="DONE", notify_emails="priya@acme.com")
        self.assertEqual(mail.outbox, [])

    def test_nobody_chosen_or_a_bad_address_is_refused_and_nothing_saved(self):
        response = self.edit(self.reel, status="DONE", notify="on")
        self.assertContains(response, "Choose who to email", status_code=400)
        response = self.edit(self.reel, status="DONE", notify="on", notify_emails="priya@acme.com, not-an-email")
        self.assertContains(response, "not-an-email", status_code=400)
        self.reel.refresh_from_db()
        self.assertEqual(self.reel.status, "PLANNED")
        self.assertEqual(mail.outbox, [])

    def test_hidden_activities_cannot_be_announced(self):
        response = self.edit(self.reel, show_to_client="", notify="on", notify_emails="priya@acme.com")
        self.assertContains(response, "hidden from the client", status_code=400)

    def test_people_who_unsubscribed_are_skipped(self):
        priya = updates.person_for(self.acme, name="Priya", email="priya@acme.com", contact=self.contact)
        self.client.post(reverse("client_review:updates", args=[priya.token]), {"frequency": "OFF"})
        self.edit(self.reel, status="DONE", notify="on",
                  notify_emails="priya@acme.com, rahul@acme.com")
        self.assertEqual([m.to for m in mail.outbox], [["rahul@acme.com"]])

    def test_form_shows_the_section_and_whatsapp_share(self):
        response = self.client.get(reverse("client_calendar:activity_edit", args=[self.reel.pk]))
        self.assertContains(response, "Email the client about this now")
        self.assertContains(response, "Priya Shah")
        self.assertContains(response, "wa.me/?text=")


class SeveralEmailsTests(NoticeTestCase):
    def test_split_emails(self):
        valid, invalid = updates.split_emails(
            "Priya <priya@acme.com>, rahul@acme.com;\nRAHUL@acme.com  brand@acme.com, nope")
        self.assertEqual(valid, ["priya@acme.com", "rahul@acme.com", "brand@acme.com"])
        self.assertEqual(invalid, ["nope"])

    def test_approval_request_to_several_pasted_addresses(self):
        self.client.post(reverse("client_calendar:approval_create", args=[self.reel.pk]), {
            "new_name": [""], "new_email": ["rahul@acme.com, brand@acme.com"],
            "content": "Caption to approve"})
        approval = ApprovalRequest.objects.get()
        self.assertEqual(sorted(r.email for r in approval.reviewers.all()),
                         ["brand@acme.com", "rahul@acme.com"])
        self.assertEqual(len(mail.outbox), 2)

    def test_update_emails_panel_adds_several_but_never_overrides_an_unsubscribe(self):
        gone = updates.person_for(self.acme, name="Gone", email="gone@acme.com")
        updates.set_frequency(gone, "OFF", by_client=True)
        self.client.post(reverse("client_calendar:update_subscribers", args=[self.acme.pk]),
                         {"new_email": "rahul@acme.com, brand@acme.com, gone@acme.com"})
        self.assertEqual(ClientReviewer.objects.get(email="rahul@acme.com").update_frequency, "SOON")
        self.assertEqual(ClientReviewer.objects.get(email="brand@acme.com").update_frequency, "SOON")
        self.assertEqual(ClientReviewer.objects.get(email="gone@acme.com").update_frequency, "OFF")

    def test_choosing_emails_again_clears_an_unsubscribe(self):
        priya = updates.person_for(self.acme, name="Priya", email="priya@acme.com")
        url = reverse("client_review:updates", args=[priya.token])
        self.client.post(url, {"frequency": "OFF"})
        priya.refresh_from_db()
        self.assertIsNotNone(priya.unsubscribed_at)
        self.client.post(url, {"frequency": "DAILY"})
        priya.refresh_from_db()
        self.assertIsNone(priya.unsubscribed_at)
