"""Calendar update emails to clients.

Recording is tested through every path that changes an activity (a plain
save, the agency calendar's drag, the planner's delete); sending is tested with
the clock passed in, because "wait for a quiet spell" and "once in the morning"
are the whole point.
"""
from datetime import date, datetime, time, timedelta
from io import StringIO

from django.core import mail
from django.core.management import call_command
from django.test import Client as HttpClient
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from calendar_hub.sources.client_activity import ClientActivitySource
from calendar_hub.tests.factories import make_user
from clients.models import Client, Contact
from projects.models import Project

from . import approvals, updates
from .models import ClientActivity, ClientCalendarLink, ClientReviewer, ClientUpdate
from .testing import ReviewClient

FUTURE = timezone.localdate() + timedelta(days=10)


def day_label(day):
    return f"{day:%a} {day.day} {day:%b}"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   SITE_URL="https://os.example.com",
                   CLIENT_UPDATES_QUIET_MINUTES=20,
                   CLIENT_UPDATES_MAX_WAIT_MINUTES=120,
                   CLIENT_UPDATES_DIGEST_HOUR=9)
class UpdatesTestCase(TestCase):
    client_class = ReviewClient

    def setUp(self):
        self.manager = make_user("mgr", role="Manager")
        self.acme = Client.objects.create(name="Acme Foods")
        self.contact = Contact.objects.create(client=self.acme, name="Priya Shah",
                                              email="priya@acme.com", is_primary=True)
        self.social = Project.objects.create(client=self.acme, name="Social media",
                                             status=Project.Status.ACTIVE)
        self.link = ClientCalendarLink.objects.create(client=self.acme, created_by=self.manager)
        self.priya = ClientReviewer.objects.create(client=self.acme, name="Priya Shah",
                                                   email="priya@acme.com", contact=self.contact)
        updates.set_frequency(self.priya, "SOON", now=timezone.now() - timedelta(hours=1))

    def plan(self, **fields):
        values = {"project": self.social, "kind": "REEL", "platform": "INSTAGRAM",
                  "date": FUTURE, "title": "Diwali reel"}
        values.update(fields)
        return ClientActivity.objects.create(**values)

    def kinds(self):
        return list(ClientUpdate.objects.order_by("id").values_list("kind", flat=True))

    def age(self, minutes):
        ClientUpdate.objects.update(created_at=timezone.now() - timedelta(minutes=minutes))


class RecordingTests(UpdatesTestCase):
    def test_new_visible_activity_is_logged_hidden_one_is_not(self):
        self.plan()
        self.plan(title="Draft", show_to_client=False)
        self.assertEqual(self.kinds(), ["ADDED"])

    def test_showing_then_hiding(self):
        activity = self.plan(show_to_client=False)
        activity.show_to_client = True
        activity.save()
        activity.show_to_client = False
        activity.save()
        self.assertEqual(self.kinds(), ["ADDED", "REMOVED"])

    def test_rescheduling_from_a_form_and_from_the_agency_calendar(self):
        activity = ClientActivity.objects.get(pk=self.plan().pk)
        activity.date = FUTURE + timedelta(days=1)
        activity.save()
        ClientActivitySource().move(self.manager, activity.pk, FUTURE + timedelta(days=3))
        moves = ClientUpdate.objects.filter(kind="MOVED").order_by("id")
        self.assertEqual([(m.old_date, m.date) for m in moves],
                         [(FUTURE, FUTURE + timedelta(days=1)),
                          (FUTURE + timedelta(days=1), FUTURE + timedelta(days=3))])

    def test_only_status_changes_a_client_would_care_about(self):
        activity = self.plan()
        for status in ("IN_PROGRESS", "APPROVAL", "APPROVED", "POSTPONED", "PLANNED", "DONE"):
            activity.status = status
            activity.save()
        self.assertEqual(self.kinds(), ["ADDED", "POSTPONED", "RESUMED", "DONE"])

    def test_archived_projects_are_silent(self):
        self.social.is_archived = True
        self.social.save()
        self.plan()
        self.assertEqual(self.kinds(), [])

    def test_deleting_from_the_planner(self):
        activity = self.plan()
        self.client.force_login(self.manager)
        self.client.post(reverse("client_calendar:activity_delete", args=[activity.pk]))
        self.assertEqual(self.kinds(), ["ADDED", "REMOVED"])
        self.assertEqual(ClientUpdate.objects.get(kind="REMOVED").title, "Diwali reel")


class SendingTests(UpdatesTestCase):
    def test_waits_for_a_quiet_spell_then_sends_once(self):
        self.plan()
        self.assertEqual(updates.run()["sent"], 0)
        self.age(25)
        self.assertEqual(updates.run()["sent"], 1)
        self.assertEqual(updates.run()["sent"], 0)

        message = mail.outbox[0]
        self.assertEqual(message.to, ["priya@acme.com"])
        self.assertIn("1 update", message.subject)
        self.assertIn("Diwali reel", message.body)
        self.assertIn("https://os.example.com" + self.link.get_absolute_url(), message.body)
        self.assertIn("/updates/unsubscribe/", message.extra_headers["List-Unsubscribe"])
        self.assertEqual(message.extra_headers["List-Unsubscribe-Post"],
                         "List-Unsubscribe=One-Click")

    def test_a_busy_day_still_sends_within_the_cap(self):
        self.plan()
        self.age(40)
        self.plan(title="Another")  # just now, so the batch isn't quiet yet
        self.assertEqual(updates.run()["sent"], 0)
        with self.settings(CLIENT_UPDATES_MAX_WAIT_MINUTES=30):
            self.assertEqual(updates.run()["sent"], 1)

    def test_several_edits_to_one_post_are_one_line(self):
        activity = self.plan()
        activity.date = FUTURE + timedelta(days=2)
        activity.save()
        activity.title = "Diwali reel, final"
        activity.save()
        self.age(25)
        updates.run()
        message = mail.outbox[0]
        self.assertIn("1 update", message.subject)
        self.assertIn("NEWLY PLANNED", message.body)
        self.assertNotIn("RESCHEDULED", message.body)
        self.assertIn("Diwali reel, final", message.body)

    def test_added_then_deleted_before_sending_says_nothing(self):
        activity = self.plan()
        updates.record_removed(activity)
        activity.delete()
        self.age(25)
        self.assertEqual(updates.run()["sent"], 0)
        self.priya.refresh_from_db()
        self.assertFalse(updates.pending(self.priya))

    def test_moves_show_old_and_new_dates(self):
        activity = self.plan()
        ClientUpdate.objects.all().delete()  # as if the addition was already emailed
        activity.date = FUTURE + timedelta(days=2)
        activity.save()
        self.age(25)
        updates.run()
        body = mail.outbox[0].body
        self.assertIn("RESCHEDULED", body)
        self.assertIn(f"was {day_label(FUTURE)}, now {day_label(FUTURE + timedelta(days=2))}", body)

    def test_hidden_work_never_appears(self):
        self.plan(title="Public reel")
        secret = self.plan(title="Secret launch")
        secret.show_to_client = False
        secret.save()
        self.age(25)
        updates.run()
        self.assertIn("Public reel", mail.outbox[0].body)
        self.assertNotIn("Secret launch", mail.outbox[0].body)

    def test_published_items_link_to_the_post(self):
        activity = self.plan()
        ClientUpdate.objects.all().delete()
        activity.status = "DONE"
        activity.link = "https://instagram.com/p/xyz"
        activity.save()
        self.age(25)
        updates.run()
        body = mail.outbox[0].body
        self.assertIn("PUBLISHED & LIVE", body)
        self.assertIn("https://instagram.com/p/xyz", body)

    def test_nothing_goes_while_the_link_is_off_and_nothing_is_lost(self):
        self.link.is_active = False
        self.link.save()
        self.plan()
        self.age(25)
        self.assertEqual(updates.run()["sent"], 0)
        self.link.is_active = True
        self.link.save()
        self.assertEqual(updates.run()["sent"], 1)

    def test_new_subscribers_start_from_now_and_off_means_off(self):
        self.plan()
        self.age(25)
        rahul = updates.person_for(self.acme, name="Rahul", email="rahul@acme.com")
        updates.set_frequency(rahul, "SOON")
        updates.person_for(self.acme, name="Nobody", email="off@acme.com")
        updates.run()
        self.assertEqual([m.to for m in mail.outbox], [["priya@acme.com"]])

    def test_daily_summary_goes_once_in_the_morning(self):
        monday = date(2026, 9, 14)

        def at(hour):
            return timezone.make_aware(datetime.combine(monday, time(hour, 0)))

        ClientReviewer.objects.filter(pk=self.priya.pk).update(
            update_frequency="DAILY", updates_since=at(0) - timedelta(days=1))
        self.plan()
        ClientUpdate.objects.update(created_at=at(7))
        self.assertEqual(updates.run(now=at(8))["sent"], 0)
        self.assertEqual(updates.run(now=at(9))["sent"], 1)
        self.plan(title="Second")
        ClientUpdate.objects.filter(title="Second").update(created_at=at(10))
        self.assertEqual(updates.run(now=at(11))["sent"], 0)

    def test_weekly_summary_waits_for_monday(self):
        tuesday = date(2026, 9, 15)
        monday = date(2026, 9, 21)
        ClientReviewer.objects.filter(pk=self.priya.pk).update(
            update_frequency="WEEKLY",
            updates_since=timezone.make_aware(datetime.combine(tuesday, time(0, 0))))
        self.plan()
        ClientUpdate.objects.update(created_at=timezone.make_aware(datetime.combine(tuesday, time(7, 0))))
        self.assertEqual(updates.run(now=timezone.make_aware(datetime.combine(tuesday, time(10, 0))))["sent"], 0)
        self.assertEqual(updates.run(now=timezone.make_aware(datetime.combine(monday, time(10, 0))))["sent"], 1)

    def test_waiting_approvals_are_mentioned(self):
        activity = self.plan()
        approvals.create_request(activity, reviewers=[self.priya], content="Caption", actor=self.manager)
        self.age(25)
        updates.run()
        self.assertIn("waiting for your approval", mail.outbox[-1].body)

    def test_command(self):
        self.plan()
        self.age(25)
        out = StringIO()
        call_command("send_client_updates", "--dry-run", stdout=out, stderr=StringIO())
        self.assertIn("Would send 1", out.getvalue())
        self.assertEqual(len(mail.outbox), 0)


class PreferenceTests(UpdatesTestCase):
    def test_page_and_saving(self):
        url = reverse("client_review:updates", args=[self.priya.token])
        response = self.client.get(url)
        self.assertContains(response, "pr•••@acme.com")
        self.assertContains(response, "As things change")
        response = self.client.post(url, {"frequency": "WEEKLY"})
        self.assertEqual(response.status_code, 302)
        self.priya.refresh_from_db()
        self.assertEqual(self.priya.update_frequency, "WEEKLY")

    def test_one_click_unsubscribe_needs_no_csrf_and_get_changes_nothing(self):
        url = reverse("client_review:unsubscribe", args=[self.priya.token])
        strict = HttpClient(enforce_csrf_checks=True)
        self.assertEqual(strict.get(url).status_code, 302)
        self.priya.refresh_from_db()
        self.assertEqual(self.priya.update_frequency, "SOON")
        self.assertEqual(strict.post(url).status_code, 200)
        self.priya.refresh_from_db()
        self.assertEqual(self.priya.update_frequency, "OFF")
        self.assertTrue(self.priya.is_active)  # approvals still reach them

    def test_unknown_token(self):
        self.assertEqual(self.client.get(reverse("client_review:updates", args=["x" * 40])).status_code, 404)
        self.assertEqual(self.client.post(reverse("client_review:unsubscribe", args=["x" * 40])).status_code, 404)


class TeamPanelTests(UpdatesTestCase):
    def test_planner_lists_contacts(self):
        Contact.objects.create(client=self.acme, name="Rahul Mehta", email="rahul@acme.com")
        self.client.force_login(self.manager)
        response = self.client.get(reverse("client_calendar:planner", args=[self.acme.pk]))
        self.assertContains(response, "Update emails")
        self.assertContains(response, "Rahul Mehta")

    def test_subscribing_a_contact_and_adding_by_email(self):
        rahul = Contact.objects.create(client=self.acme, name="Rahul Mehta", email="rahul@acme.com")
        self.client.force_login(self.manager)
        self.client.post(reverse("client_calendar:update_subscribers", args=[self.acme.pk]), {
            f"freq_c{rahul.pk}": "DAILY", f"freq_c{self.contact.pk}": "SOON",
            "new_name": "Brand team", "new_email": "Brand@Acme.com",
        })
        reviewer = ClientReviewer.objects.get(email="rahul@acme.com")
        self.assertEqual(reviewer.update_frequency, "DAILY")
        self.assertIsNotNone(reviewer.updates_since)
        self.assertEqual(ClientReviewer.objects.get(email="brand@acme.com").update_frequency, "SOON")

    def test_changing_emails_does_not_restore_revoked_approval_access(self):
        ClientReviewer.objects.filter(pk=self.priya.pk).update(is_active=False)
        self.client.force_login(self.manager)
        self.client.post(reverse("client_calendar:update_subscribers", args=[self.acme.pk]),
                         {f"freq_c{self.contact.pk}": "DAILY"})
        self.priya.refresh_from_db()
        self.assertEqual(self.priya.update_frequency, "DAILY")
        self.assertFalse(self.priya.is_active)

    def test_people_without_access_cannot_change_it(self):
        self.client.force_login(make_user("hr", role="HR"))
        self.client.post(reverse("client_calendar:update_subscribers", args=[self.acme.pk]),
                         {f"freq_c{self.contact.pk}": "OFF"})
        self.priya.refresh_from_db()
        self.assertEqual(self.priya.update_frequency, "SOON")
