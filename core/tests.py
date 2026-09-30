from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import AgencySettings


class OpsLayerTests(TestCase):
    """M7: agency settings screen gating + activity/notifications pages."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user("owner", password="x", role="OWNER")
        cls.sales = User.objects.create_user("sales", password="x", role="SALES")

    def test_settings_screen_owner_only(self):
        self.client.force_login(self.sales)
        r = self.client.get(reverse("core:settings"))
        self.assertEqual(r.status_code, 302)  # redirected away
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("core:settings")).status_code, 200)

    def test_settings_save(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("core:settings"), {
            "agency_name": "Livo Digital Studio", "tagline": "Web that works",
            "primary_color": "#00795b", "email": "hi@livodigital.com",
            "phone": "99999", "address": "Hyderabad", "gstin": "36X", "website": "",
        })
        self.assertEqual(AgencySettings.load().agency_name, "Livo Digital Studio")

    def test_activity_log_is_super_admin_only(self):
        # Sales (not a Super Admin) is redirected away…
        self.client.force_login(self.sales)
        self.assertEqual(self.client.get(reverse("core:activity")).status_code, 302)
        # …the owner (OWNER -> Super Admin) gets in.
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("core:activity")).status_code, 200)

    def test_notifications_open_to_all(self):
        self.client.force_login(self.sales)
        self.assertEqual(self.client.get(reverse("core:notifications")).status_code, 200)

    def _manager(self):
        from accounts.models import Role
        mgr = get_user_model().objects.create_user("mgr", password="x")
        mgr.primary_role = Role.objects.get(name="Manager")
        mgr.save(update_fields=["primary_role"])  # overrides the OWNER default
        return mgr

    def test_admin_section_hidden_from_non_admins(self):
        self.client.force_login(self.sales)
        body = self.client.get(reverse("core:dashboard")).content.decode()
        # the whole Admin group is gone
        self.assertNotIn(">Admin<", body)
        self.assertNotIn("Templates", body)
        self.assertNotIn("Activity log", body)
        self.assertNotIn('href="/admin/"', body)
        self.assertIn("My account", body)   # Account section is there instead

    def test_manager_sees_admin_section_but_not_activity_log(self):
        self.client.force_login(self._manager())
        body = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertIn("Templates", body)         # Admin section visible to Manager
        self.assertNotIn("Activity log", body)   # …but the log is Super Admin only

    def test_super_admin_sees_admin_section_and_activity_log(self):
        self.client.force_login(self.owner)  # OWNER -> Super Admin
        body = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertIn("Templates", body)
        self.assertIn("Activity log", body)

    def test_notifications_marked_read_on_open(self):
        from .models import Notification, notify
        notify([self.sales], "Test ping", "/")
        self.assertEqual(self.sales.notifications.filter(is_read=False).count(), 1)
        self.client.force_login(self.sales)
        self.client.get(reverse("core:notifications"))
        self.assertEqual(self.sales.notifications.filter(is_read=False).count(), 0)
