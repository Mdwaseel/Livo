"""The template-page upload exemption: bigger files, no downscale, one screen.

The three template page images (cover, content background, back) are full-bleed
A4 print designs that every future document is generated on. The app-wide 5 MB
ceiling refuses a normal A4 export outright, and the 2000px downscale silently
resamples the letterhead — a loss nobody sees until a client is reading the PDF.

Lifting a limit is easy to get wrong in three directions, so each has a test:

1. it must actually be lifted, here
2. it must NOT be lifted anywhere else
3. it must not lift anything other than the size — the extension allow-list and
   the CSRF check both have to survive, and the CSRF one is load-bearing
   because `uncapped_uploads` works by exempting the view from the middleware
   and re-running the check itself. A mistake there turns a state-changing
   admin screen into an unauthenticated one.
"""
import io

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import Role
from core.uploads import process_upload
from django.contrib.auth import get_user_model

from .models import Template

User = get_user_model()
PASSWORD = "template-upload-test-99"


def a4_png(width=2480, height=3508, name="cover.png"):
    """An A4-at-300-DPI design. Noise rather than flat colour so it does not
    compress away to nothing and the size assertions stay meaningful."""
    from PIL import Image

    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for x in range(0, width, 5):
        for y in range(0, height, 5):
            pixels[x, y] = ((x * 7) % 256, (y * 13) % 256, (x * y) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class TemplateCeilingIsLifted(TestCase):
    """The exemption does what it says, at the `process_upload` level."""

    def test_a_file_over_the_global_ceiling_is_accepted(self):
        blob = SimpleUploadedFile("cover.png", b"x" * (9 * 1024 * 1024),
                                  content_type="image/png")
        with override_settings(MAX_UPLOAD_BYTES=5 * 1024 * 1024):
            # No ceiling: exactly what TEMPLATE_UPLOAD_MAX_BYTES=0 passes in.
            result = process_upload(blob, compress=False, max_bytes=0)
        self.assertEqual(result.size, 9 * 1024 * 1024)

    def test_the_same_file_is_still_refused_on_a_normal_path(self):
        """The whole point of the change is that it reaches ONE screen."""
        blob = SimpleUploadedFile("dump.png", b"x" * (9 * 1024 * 1024),
                                  content_type="image/png")
        with override_settings(MAX_UPLOAD_BYTES=5 * 1024 * 1024):
            with self.assertRaises(ValidationError) as caught:
                process_upload(blob, compress=False)
        self.assertIn("Drive link", caught.exception.messages[0])

    def test_max_edge_zero_leaves_the_design_at_full_resolution(self):
        """A 2480x3508 A4 page must come out 2480x3508, not 1414x2000."""
        from PIL import Image

        original = a4_png()
        result = process_upload(original, max_bytes=0, max_edge=0)
        result.seek(0)
        self.assertEqual(Image.open(result).size, (2480, 3508))

    def test_the_default_path_still_downscales(self):
        from PIL import Image

        with override_settings(IMAGE_MAX_EDGE=2000, MAX_UPLOAD_BYTES=50 * 1024 * 1024):
            result = process_upload(a4_png())
        result.seek(0)
        self.assertEqual(max(Image.open(result).size), 2000)

    def test_an_explicit_ceiling_is_still_honoured(self):
        """0 means unlimited; a real number must still bite, or the setting is
        decoration."""
        blob = SimpleUploadedFile("cover.png", b"x" * (3 * 1024 * 1024),
                                  content_type="image/png")
        with self.assertRaises(ValidationError):
            process_upload(blob, compress=False, max_bytes=1024 * 1024)


class TheExemptionDoesNotLiftAnythingElse(TestCase):
    """Size is a disk-space policy. The allow-list is the security control."""

    def test_the_extension_allow_list_still_applies_with_no_ceiling(self):
        blob = SimpleUploadedFile("payload.exe", b"MZ" * 100,
                                  content_type="application/octet-stream")
        with self.assertRaises(ValidationError) as caught:
            process_upload(blob, compress=False, max_bytes=0, max_edge=0)
        self.assertIn("accepted type", caught.exception.messages[0])

    def test_an_svg_is_still_stored_as_is_not_re_encoded(self):
        """SVG is XML and can carry script; it is size-checked, never parsed by
        Pillow. Lifting the ceiling must not change that."""
        blob = SimpleUploadedFile("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>",
                                  content_type="image/svg+xml")
        result = process_upload(blob, max_bytes=0, max_edge=0)
        self.assertEqual(result.name, "logo.svg")


class TheTemplateScreenStillChecksCsrf(TestCase):
    """`uncapped_uploads` exempts the view from CsrfViewMiddleware so it can
    swap the upload handlers, then re-applies the check by hand. If that second
    half ever breaks, this admin screen silently accepts forged posts."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            "tplowner", password=PASSWORD,
            primary_role=Role.objects.get(name="Super Admin"))

    def test_a_post_without_a_token_is_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        response = client.post(reverse("doc_templates:create"),
                               {"name": "Forged"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Template.objects.filter(name="Forged").exists())

    def test_a_post_with_a_token_is_accepted(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        # Fetch the form to obtain a real token, exactly as a browser would.
        client.get(reverse("doc_templates:create"))
        token = client.cookies["csrftoken"].value
        response = client.post(reverse("doc_templates:create"),
                               {"name": "Genuine", "csrfmiddlewaretoken": token})
        self.assertIn(response.status_code, (200, 302))
        self.assertTrue(Template.objects.filter(name="Genuine").exists())

    def test_the_permission_gate_still_applies(self):
        """csrf_exempt must not have swallowed the auth decorators under it."""
        nobody = User.objects.create_user(
            "tpldev", password=PASSWORD,
            primary_role=Role.objects.get(name="Developer"))
        self.client.force_login(nobody)
        response = self.client.post(reverse("doc_templates:create"),
                                    {"name": "Sneaky"})
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(Template.objects.filter(name="Sneaky").exists())

    def test_anonymous_cannot_post(self):
        response = self.client.post(reverse("doc_templates:create"),
                                    {"name": "Anon"})
        self.assertFalse(Template.objects.filter(name="Anon").exists())


class TheUploadHandlersAreSwappedOnlyHere(TestCase):
    """The cap is enforced by a handler installed globally in settings. The
    exemption removes it for this view; every other view must keep it."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            "tplowner2", password=PASSWORD,
            primary_role=Role.objects.get(name="Super Admin"))

    def test_the_capped_handler_is_gone_on_the_template_post(self):
        seen = {}
        from documents import views_templates

        original = views_templates._template_form

        def spy(request, template):
            seen["handlers"] = [type(h).__name__ for h in request.upload_handlers]
            return original(request, template)

        views_templates._template_form = spy
        try:
            self.client.force_login(self.owner)
            self.client.post(reverse("doc_templates:create"), {"name": "Spy"})
        finally:
            views_templates._template_form = original

        self.assertIn("handlers", seen)
        self.assertNotIn("CappedFileUploadHandler", seen["handlers"])
        self.assertIn("MemoryFileUploadHandler", seen["handlers"])

    def test_another_view_still_has_the_capped_handler(self):
        from django.conf import settings

        self.assertIn("core.uploads.CappedFileUploadHandler",
                      settings.FILE_UPLOAD_HANDLERS)


class TheDesignIsStoredExactly(TestCase):
    """Lifting the pixel cap alone was not enough.

    `compress_image` converts an opaque PNG to JPEG at IMAGE_QUALITY whatever
    its dimensions, so a print master came back with the right size and visible
    banding. With TEMPLATE_IMAGE_MAX_EDGE=0 nothing is re-encoded at all.
    """

    def test_an_opaque_png_is_not_silently_turned_into_a_jpeg(self):
        original = a4_png(name="letterhead.png")
        result = process_upload(original, compress=False, max_bytes=0, max_edge=0)
        self.assertTrue(result.name.endswith(".png"))

    def test_the_bytes_are_stored_untouched(self):
        original = a4_png(name="letterhead.png")
        original.seek(0)
        before = original.read()
        original.seek(0)
        result = process_upload(original, compress=False, max_bytes=0, max_edge=0)
        result.seek(0)
        self.assertEqual(result.read(), before)

    def test_setting_a_pixel_cap_turns_re_encoding_back_on(self):
        """The escape hatch has to actually work, or it is decoration."""
        from PIL import Image

        result = process_upload(a4_png(), compress=True, max_bytes=0,
                                max_edge=1000)
        result.seek(0)
        self.assertEqual(max(Image.open(result).size), 1000)
