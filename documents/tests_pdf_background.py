"""The content background must cover the sheet, not the text area.

The content region is expressed as `@page` margins, and a page box's background
is positioned against its padding box — which those margins have already inset.
So `background-position: 0 0` started the letterhead at the top-left corner of
the TEXT area rather than of the paper: the whole design slid down by `top` and
right by `left`, and whatever fell past the far edge was cropped.

On the real letterhead that meant a logo sitting at 7% of the page printed at
19%, behind the first line of text, and the contact bar at 93% printed past the
bottom edge and vanished. The page still looked plausible, which is why it took
a side-by-side against the source image to see.

These tests pin the relationship rather than the string: the offset must always
equal the margins, whatever the region is set to. Reverting to `0 0` fails all
of them.
"""
import io
import re
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from clients.models import Client
from projects.models import Project

from .models import Document, DocumentType, Template
from .pdf import A4_MM, build_visual_html_pisa, build_visual_html_weasy
from .services import resolve_template


def png(name):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1414, 2000), (255, 255, 255)).save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class BackgroundCoversTheSheet(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.doc_type = DocumentType.objects.create(name="Invoice", slug="invoice")
        client = Client.objects.create(name="Acme")
        project = Project.objects.create(client=client, name="Job")
        cls.document = Document.objects.create(
            project=project, document_type=cls.doc_type, title="INV-1")

    def _css(self, x, y, w, h):
        Template.objects.all().delete()
        Template.objects.create(
            name="Letterhead", document_type=None,
            content_background=png("bg.png"),
            content_x=x, content_y=y, content_width=w, content_height=h)
        pages = resolve_template(self.doc_type)
        return build_visual_html_weasy(self.document, pages)

    @staticmethod
    def _offsets(css):
        match = re.search(
            r"background-position:\s*(-?[\d.]+)mm\s+(-?[\d.]+)mm", css)
        assert match, "no background-position in the generated CSS"
        return float(match.group(1)), float(match.group(2))

    @staticmethod
    def _margins(css):
        match = re.search(
            r"@page content \{[^}]*?margin:\s*([\d.]+)mm\s+([\d.]+)mm\s+"
            r"([\d.]+)mm\s+([\d.]+)mm", css, re.S)
        assert match, "no @page content margin in the generated CSS"
        top, right, bottom, left = (float(g) for g in match.groups())
        return top, right, bottom, left

    def test_the_offset_cancels_the_margins_exactly(self):
        """The invariant. Margin pushes the background in; the offset has to
        pull it back out by the same amount, or the design is not on the paper
        where it was designed to be."""
        css = self._css(8, 12, 84, 81.62)
        offset_x, offset_y = self._offsets(css)
        top, _, _, left = self._margins(css)
        self.assertAlmostEqual(offset_x, -left, places=2)
        self.assertAlmostEqual(offset_y, -top, places=2)

    def test_it_holds_for_a_completely_different_region(self):
        css = self._css(20, 30, 60, 50)
        offset_x, offset_y = self._offsets(css)
        top, _, _, left = self._margins(css)
        self.assertAlmostEqual(offset_x, -left, places=2)
        self.assertAlmostEqual(offset_y, -top, places=2)

    def test_the_offset_is_derived_from_the_region_not_hardcoded(self):
        """Two different regions must produce two different offsets. A constant
        `0 0` — the original bug — passes neither this nor the two above."""
        first = self._offsets(self._css(5, 5, 90, 90))
        second = self._offsets(self._css(25, 40, 50, 40))
        self.assertNotEqual(first, second)

    def test_a_full_bleed_region_needs_no_offset(self):
        """The boundary: margins of zero mean the padding box already IS the
        sheet, so the correct offset is zero. Proves the rule is arithmetic
        rather than a fudge factor."""
        offset_x, offset_y = self._offsets(self._css(0, 0, 100, 100))
        self.assertEqual((offset_x, offset_y), (-0.0, -0.0))

    def test_the_background_is_still_sized_to_the_whole_sheet(self):
        """Offsetting without full-sheet sizing would just move the crop."""
        w_mm, h_mm = A4_MM
        css = self._css(8, 12, 84, 81.62)
        self.assertIn(f"background-size: {w_mm}mm {h_mm}mm", css)
        self.assertIn("background-repeat: no-repeat", css)

    def test_the_content_region_is_unchanged_by_the_fix(self):
        """The text must stay exactly where it was drawn — this fix moves the
        background, never the content."""
        css = self._css(8, 12, 84, 81.62)
        w_mm, h_mm = A4_MM
        top, right, bottom, left = self._margins(css)
        self.assertAlmostEqual(left, 8 / 100 * w_mm, places=2)
        self.assertAlmostEqual(top, 12 / 100 * h_mm, places=2)
        self.assertAlmostEqual(right, w_mm - (8 + 84) / 100 * w_mm, places=2)
        self.assertAlmostEqual(bottom, h_mm - (12 + 81.62) / 100 * h_mm, places=2)


class TheFallbackEngineWasAlreadyRight(TestCase):
    """xhtml2pdf places the region with an @frame on a zero-margin page, so its
    background was never inset. Pinned so a future tidy-up does not 'fix' it
    into the same bug."""

    def test_the_pisa_page_has_no_margin_to_offset(self):
        doc_type = DocumentType.objects.create(name="Invoice", slug="invoice")
        client = Client.objects.create(name="Acme")
        project = Project.objects.create(client=client, name="Job")
        document = Document.objects.create(
            project=project, document_type=doc_type, title="INV-1")
        Template.objects.create(
            name="Letterhead", document_type=None,
            content_background=png("bg.png"),
            content_x=8, content_y=12, content_width=84, content_height=76)

        css = build_visual_html_pisa(document, resolve_template(doc_type))
        self.assertIn("margin: 0pt", css)
        self.assertIn("@frame content", css)
