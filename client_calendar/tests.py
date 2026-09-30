"""Client calendar.

Most of this file is about the public link, because it is the one page in the
app that answers to nobody signed in. Every test there is written the way an
outsider would reach it — by URL, unauthenticated, editing parameters — rather
than by checking what a template happens to hide.
"""
import io
import shutil
import tempfile
from datetime import date, time

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from calendar_hub.tests.factories import make_user
from clients.models import Client
from projects.models import Project

from . import services
from .models import ClientActivity, ClientCalendarLink

SEPT = date(2026, 9, 1)
TODAY = date(2026, 9, 13)


def png_bytes():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (15, 138, 128)).save(buffer, "PNG")
    return buffer.getvalue()


class Fixture:
    def __init__(self):
        self.manager = make_user("mgr", role="Manager")
        self.designer = make_user("designer", role="Designer")
        self.sales = make_user("sales", role="Sales")
        self.hr = make_user("hr", role="HR")

        self.acme = Client.objects.create(name="Acme Foods")
        self.globex = Client.objects.create(name="Globex")
        self.social = Project.objects.create(client=self.acme, name="Social media",
                                             status=Project.Status.ACTIVE)
        self.ads = Project.objects.create(client=self.acme, name="Performance ads",
                                          status=Project.Status.ACTIVE)
        self.rival = Project.objects.create(client=self.globex, name="Globex social",
                                            status=Project.Status.ACTIVE)
        # The designer works on Acme's social account only.
        self.social.members.add(self.designer, self.sales)

        self.reel = ClientActivity.objects.create(
            project=self.social, kind="REEL", platform="INSTAGRAM",
            status="DONE", date=date(2026, 9, 10), time=time(18, 30),
            title="Diwali offer reel", link="https://instagram.com/p/abc")
        self.approval = ClientActivity.objects.create(
            project=self.social, kind="CREATIVE", status="APPROVAL",
            date=date(2026, 9, 15), title="Festive carousel artwork")
        self.draft = ClientActivity.objects.create(
            project=self.social, kind="POST", date=date(2026, 9, 16),
            title="Internal draft do not show", show_to_client=False)
        self.campaign = ClientActivity.objects.create(
            project=self.ads, kind="AD_CHANGE", platform="META_ADS",
            date=date(2026, 9, 18), title="Retargeting budget increase")
        self.secret = ClientActivity.objects.create(
            project=self.rival, kind="POST", date=date(2026, 9, 10),
            title="Globex launch teaser")

        self.link = ClientCalendarLink.objects.create(client=self.acme,
                                                      created_by=self.manager)


def public_url(link, **params):
    from urllib.parse import urlencode

    url = reverse("client_calendar_public:calendar", args=[link.token])
    return url + ("?" + urlencode(params) if params else "")


# ---------------------------------------------------------------------------
# the public link
# ---------------------------------------------------------------------------

class PublicLinkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Fixture()

    def get(self, **params):
        params.setdefault("date", "2026-09")
        return self.client.get(public_url(self.data.link, **params))

    def test_opens_without_signing_in_and_shows_the_plan(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Acme Foods")
        self.assertContains(response, "Diwali offer reel")
        self.assertContains(response, "Retargeting budget increase")

    def test_drafts_marked_hidden_never_reach_the_client(self):
        self.assertNotContains(self.get(), "Internal draft do not show")

    def test_another_clients_activities_never_appear(self):
        self.assertNotContains(self.get(), "Globex launch teaser")

    def test_editing_the_project_parameter_cannot_reach_another_client(self):
        response = self.get(project=self.data.rival.pk)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Globex launch teaser")
        # Ignored rather than obeyed: the page falls back to all of Acme.
        self.assertContains(response, "Diwali offer reel")

    def test_client_wording_for_status(self):
        response = self.get()
        self.assertContains(response, "Published")
        self.assertContains(response, "Needs your approval")
        self.assertNotContains(response, "Awaiting client approval")

    def test_unknown_token_is_a_404_that_names_nobody(self):
        response = self.client.get(
            reverse("client_calendar_public:calendar", args=["x" * 32]))
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "Acme", status_code=404)

    def test_a_turned_off_link_is_a_410_that_names_nobody(self):
        self.data.link.is_active = False
        self.data.link.save()
        response = self.get()
        self.assertEqual(response.status_code, 410)
        self.assertContains(response, "no longer active", status_code=410)
        self.assertNotContains(response, "Acme", status_code=410)
        self.assertNotContains(response, "Diwali", status_code=410)

    def test_rotating_kills_the_old_url_immediately(self):
        old_url = public_url(self.data.link, date="2026-09")
        self.data.link.rotate()
        self.assertEqual(self.client.get(old_url).status_code, 404)
        self.assertEqual(self.get().status_code, 200)

    def test_an_archived_client_takes_its_link_down(self):
        self.data.acme.is_archived = True
        self.data.acme.save()
        self.assertEqual(self.get().status_code, 410)

    def test_archived_projects_drop_off_the_page(self):
        self.data.ads.is_archived = True
        self.data.ads.save()
        self.assertNotContains(self.get(), "Retargeting budget increase")

    def test_headers_keep_it_out_of_search_and_out_of_referers(self):
        response = self.get()
        self.assertIn("noindex", response["X-Robots-Tag"])
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", response["Cache-Control"])

    def test_external_links_do_not_leak_the_token(self):
        self.assertContains(self.get(), 'rel="noopener noreferrer"')

    def test_client_opens_are_counted_but_team_previews_are_not(self):
        self.get()
        self.data.link.refresh_from_db()
        self.assertEqual(self.data.link.view_count, 1)
        self.assertIsNotNone(self.data.link.last_viewed_at)

        self.client.force_login(self.data.manager)
        self.get()
        self.data.link.refresh_from_db()
        self.assertEqual(self.data.link.view_count, 1)

    def test_tokens_are_long_random_and_unique(self):
        other = ClientCalendarLink.objects.create(client=self.data.globex)
        self.assertGreaterEqual(len(self.data.link.token), 32)
        self.assertNotEqual(self.data.link.token, other.token)

    def test_an_empty_month_says_so(self):
        response = self.get(date="2030-01")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "is still being planned")

    def test_junk_dates_render_today_instead_of_failing(self):
        for junk in ("banana", "0001-01", "2026-13", "9999-99-99"):
            with self.subTest(date=junk):
                self.assertEqual(self.get(date=junk).status_code, 200)

    def test_the_approval_filter_shows_only_what_is_waiting(self):
        response = self.get(status="APPROVAL", view="list")
        self.assertContains(response, "Festive carousel artwork")
        self.assertNotContains(response, "Diwali offer reel")

    def test_a_type_filter_narrows_but_keeps_every_chip_count(self):
        response = self.get(type="ads")
        self.assertContains(response, "Retargeting budget increase")
        self.assertNotContains(response, "Diwali offer reel")
        families = {row["key"]: row["count"] for row in response.context["families"]}
        self.assertEqual(families["content"], 1)  # the reel, still counted

    def test_post_is_refused(self):
        response = self.client.post(public_url(self.data.link))
        self.assertEqual(response.status_code, 405)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="cc-test-media-"))
class PreviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Fixture()
        for activity in (cls.data.reel, cls.data.draft, cls.data.secret):
            activity.preview = SimpleUploadedFile("working-title.png", png_bytes(),
                                                  content_type="image/png")
            activity.save()

    @classmethod
    def tearDownClass(cls):
        from django.conf import settings

        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def url(self, activity, link=None):
        return reverse("client_calendar_public:preview",
                       args=[(link or self.data.link).token, activity.pk])

    def test_the_uploaded_name_is_not_kept(self):
        self.assertNotIn("working-title", self.data.reel.preview.name)

    def test_a_visible_preview_is_served_through_the_token(self):
        response = self.client.get(self.url(self.data.reel))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_a_hidden_draft_preview_is_not(self):
        self.assertEqual(self.client.get(self.url(self.data.draft)).status_code, 404)

    def test_another_clients_preview_is_not_reachable_with_this_token(self):
        self.assertEqual(self.client.get(self.url(self.data.secret)).status_code, 404)

    def test_a_turned_off_link_serves_no_previews(self):
        self.data.link.is_active = False
        self.data.link.save()
        self.assertEqual(self.client.get(self.url(self.data.reel)).status_code, 404)


# ---------------------------------------------------------------------------
# the month shape
# ---------------------------------------------------------------------------

class ServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Fixture()

    def build(self, **params):
        params.setdefault("date", "2026-09")
        base = ClientActivity.objects.filter(project__client=self.data.acme,
                                             show_to_client=True)
        projects = Project.objects.filter(client=self.data.acme)
        return services.build(base, params, projects=projects, today=TODAY)

    def test_the_grid_is_whole_weeks(self):
        shape = self.build()
        self.assertTrue(all(len(week) == 7 for week in shape["weeks"]))
        self.assertEqual(shape["weeks"][0][0]["date"].isoweekday(), 1)

    def test_summary_counts_the_month(self):
        summary = self.build()["summary"]
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["approval"], 1)
        self.assertEqual(summary["percent"], 33)

    def test_postponed_work_leaves_the_denominator(self):
        ClientActivity.objects.filter(pk=self.data.campaign.pk).update(status="POSTPONED")
        summary = self.build()["summary"]
        self.assertEqual(summary["percent"], 50)

    def test_upcoming_counts_open_work_in_the_next_week(self):
        # 15th (approval) and 18th (planned) fall within 13–19 Sept; the done
        # reel on the 10th does not.
        self.assertEqual(self.build()["upcoming"], 2)

    def test_filter_links_keep_the_month(self):
        shape = self.build(type="ads")
        self.assertIn("date=2026-09", shape["all_types_url"])
        self.assertIn("type=ads", shape["next_url"])

    def test_parse_anchor(self):
        self.assertEqual(services.parse_anchor("2026-09", TODAY), SEPT)
        self.assertEqual(services.parse_anchor("2026-09-20", TODAY), date(2026, 9, 20))
        self.assertEqual(services.parse_anchor("nope", TODAY), TODAY)
        self.assertEqual(services.parse_anchor("1800-01-01", TODAY), TODAY)


# ---------------------------------------------------------------------------
# the planner
# ---------------------------------------------------------------------------

class PlannerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Fixture()

    def planner(self, client):
        return reverse("client_calendar:planner", args=[client.pk]) + "?date=2026-09"

    def test_manager_sees_drafts_on_the_planner(self):
        self.client.force_login(self.data.manager)
        response = self.client.get(self.planner(self.data.acme))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Internal draft do not show")
        self.assertContains(response, "Hidden from client")

    def test_no_projects_view_no_planner(self):
        self.client.force_login(self.data.hr)
        response = self.client.get(self.planner(self.data.acme))
        self.assertRedirects(response, reverse("core:dashboard"),
                             fetch_redirect_response=False)

    def test_a_designer_only_sees_the_projects_they_are_on(self):
        self.client.force_login(self.data.designer)
        response = self.client.get(self.planner(self.data.acme))
        self.assertContains(response, "Diwali offer reel")
        self.assertNotContains(response, "Retargeting budget increase")
        self.assertEqual(self.client.get(self.planner(self.data.globex)).status_code, 404)

    def test_planning_an_activity(self):
        self.client.force_login(self.data.designer)
        response = self.client.post(
            reverse("client_calendar:activity_create", args=[self.data.acme.pk]),
            {"project": self.data.social.pk, "kind": "STORY", "platform": "INSTAGRAM",
             "status": "PLANNED", "date": "2026-09-21", "time": "10:00",
             "title": "Behind the scenes story", "show_to_client": "on"})
        activity = ClientActivity.objects.get(title="Behind the scenes story")
        self.assertEqual(activity.created_by, self.data.designer)
        self.assertTrue(activity.show_to_client)
        self.assertTrue(response["Location"].endswith("#d-2026-09-21"))

    def test_mistakes_come_back_next_to_their_fields(self):
        self.client.force_login(self.data.manager)
        response = self.client.post(
            reverse("client_calendar:activity_create", args=[self.data.acme.pk]),
            {"project": self.data.social.pk, "kind": "POST", "status": "PLANNED",
             "date": "not-a-date", "title": "", "link": "instagram dot com"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.context["errors"]), {"date", "title", "link"})
        self.assertEqual(ClientActivity.objects.count(), 5)

    def test_a_project_from_another_client_is_refused(self):
        self.client.force_login(self.data.manager)
        response = self.client.post(
            reverse("client_calendar:activity_create", args=[self.data.acme.pk]),
            {"project": self.data.rival.pk, "kind": "POST", "status": "PLANNED",
             "date": "2026-09-21", "title": "Wrong client"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ClientActivity.objects.filter(title="Wrong client").exists())

    def test_view_only_roles_cannot_plan(self):
        self.client.force_login(self.data.sales)
        self.client.post(
            reverse("client_calendar:activity_create", args=[self.data.acme.pk]),
            {"project": self.data.social.pk, "kind": "POST", "status": "PLANNED",
             "date": "2026-09-21", "title": "Sales should not add this"})
        self.assertFalse(ClientActivity.objects.filter(
            title="Sales should not add this").exists())

    def test_editing_and_removing(self):
        self.client.force_login(self.data.manager)
        url = reverse("client_calendar:activity_edit", args=[self.data.approval.pk])
        self.client.post(url, {
            "project": self.data.social.pk, "kind": "CREATIVE", "status": "DONE",
            "date": "2026-09-15", "title": "Festive carousel artwork"})
        self.data.approval.refresh_from_db()
        self.assertEqual(self.data.approval.status, "DONE")
        # Unticked on save means hidden — the checkbox was not sent.
        self.assertFalse(self.data.approval.show_to_client)

        self.client.post(reverse("client_calendar:activity_delete",
                                 args=[self.data.approval.pk]))
        self.assertFalse(ClientActivity.objects.filter(pk=self.data.approval.pk).exists())

    def test_only_client_editors_manage_the_link(self):
        url = reverse("client_calendar:link_action", args=[self.data.acme.pk])
        token = self.data.link.token

        self.client.force_login(self.data.designer)  # clients.view only
        self.client.post(url, {"action": "rotate"})
        self.data.link.refresh_from_db()
        self.assertEqual(self.data.link.token, token)

        self.client.force_login(self.data.manager)
        self.client.post(url, {"action": "rotate"})
        self.data.link.refresh_from_db()
        self.assertNotEqual(self.data.link.token, token)

        self.client.post(url, {"action": "disable"})
        self.data.link.refresh_from_db()
        self.assertFalse(self.data.link.is_active)

        self.client.post(url, {"action": "enable"})
        self.data.link.refresh_from_db()
        self.assertTrue(self.data.link.is_active)

    def test_creating_a_link(self):
        self.client.force_login(self.data.manager)
        Project.objects.create(client=self.data.globex, name="More", status="ACTIVE")
        self.client.post(reverse("client_calendar:link_action", args=[self.data.globex.pk]),
                         {"action": "create"})
        self.assertTrue(ClientCalendarLink.objects.filter(client=self.data.globex,
                                                          is_active=True).exists())

    def test_the_add_form_opens_for_a_client_with_several_projects(self):
        """Acme has two projects, so nothing is preselected — that path must
        render the form, not fail on an empty foreign key."""
        self.client.force_login(self.data.manager)
        response = self.client.get(
            reverse("client_calendar:activity_create", args=[self.data.acme.pk])
            + "?date=2026-09-21")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose…")
        self.assertContains(response, 'value="2026-09-21"')

    def test_the_client_page_links_to_the_calendar(self):
        self.client.force_login(self.data.manager)
        response = self.client.get(reverse("clients:detail", args=[self.data.acme.pk]))
        self.assertContains(response, "Client calendar")
        self.assertContains(response,
                            reverse("client_calendar:planner", args=[self.data.acme.pk]))

    def test_the_index_lists_clients_with_their_month(self):
        self.client.force_login(self.data.manager)
        response = self.client.get(reverse("client_calendar:index"))
        self.assertContains(response, "Acme Foods")
        self.assertContains(response, "Shared")


class AgencyCalendarTests(TestCase):
    """The team sees client content on the agency calendar too."""

    @classmethod
    def setUpTestData(cls):
        cls.data = Fixture()

    def test_activities_appear_on_the_agency_calendar(self):
        self.client.force_login(self.data.manager)
        response = self.client.get(reverse("calendar_hub:month") + "?date=2026-09-01")
        self.assertContains(response, "Diwali offer reel")

    def test_dragging_one_reschedules_it(self):
        from calendar_hub import sources

        source = sources.get_source("client_activity")
        source.move(self.data.manager, self.data.reel.pk, date(2026, 9, 12))
        self.data.reel.refresh_from_db()
        self.assertEqual(self.data.reel.date, date(2026, 9, 12))

    def test_a_view_only_role_cannot_drag(self):
        from django.core.exceptions import PermissionDenied

        from calendar_hub import sources

        source = sources.get_source("client_activity")
        with self.assertRaises(PermissionDenied):
            source.move(self.data.sales, self.data.reel.pk, date(2026, 9, 12))
