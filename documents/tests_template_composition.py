"""Universal content and thank-you pages: each slot resolved on its own.

The natural way to set this app up is one template per document type carrying
that type's cover, plus a "Content Page" and a "Thank You Page" with no type,
meant to apply to everything. That is exactly how this install was configured
and it did not work: resolution picked ONE template, the type's cover-only
template won outright, and the two universal pages were never reached. Every
document printed a cover and nothing else.

These tests pin the composed behaviour, and the first one reproduces the
original report directly.
"""
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from .models import DocumentType, Template
from .services import resolve_template


def png(name):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 56), (200, 200, 200)).save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class UniversalPagesReachEveryType(TestCase):
    """The reported setup, rebuilt: per-type covers plus two shared pages."""

    @classmethod
    def setUpTestData(cls):
        cls.invoice = DocumentType.objects.create(name="Invoice", slug="invoice")
        cls.srs = DocumentType.objects.create(name="SRS", slug="srs")

        cls.invoice_cover = Template.objects.create(
            name="Invoice", document_type=cls.invoice,
            cover_image=png("invoice-cover.png"))
        cls.srs_cover = Template.objects.create(
            name="SRS", document_type=cls.srs, cover_image=png("srs-cover.png"))

        # No document_type: these are the "for everything" pages.
        cls.universal_content = Template.objects.create(
            name="Content Page", document_type=None,
            content_background=png("content-bg.png"),
            content_x=10, content_y=15, content_width=80, content_height=70)
        cls.universal_back = Template.objects.create(
            name="Thank You Page", document_type=None,
            back_image=png("thanks.png"))

    def test_a_cover_only_type_still_gets_the_shared_content_and_back_pages(self):
        pages = resolve_template(self.invoice)
        self.assertTrue(pages.cover_image, "the type's own cover is missing")
        self.assertTrue(pages.content_background,
                        "the universal content page never reached this type")
        self.assertTrue(pages.back_image,
                        "the universal thank-you page never reached this type")

    def test_each_slot_comes_from_the_right_template(self):
        pages = resolve_template(self.invoice)
        self.assertEqual(pages.sources["cover_image"], self.invoice_cover)
        self.assertEqual(pages.sources["content_background"],
                         self.universal_content)
        self.assertEqual(pages.sources["back_image"], self.universal_back)

    def test_every_type_gets_the_same_shared_pages(self):
        for doc_type in (self.invoice, self.srs):
            with self.subTest(type=doc_type.name):
                pages = resolve_template(doc_type)
                self.assertEqual(pages.sources["back_image"], self.universal_back)
                self.assertEqual(pages.sources["content_background"],
                                 self.universal_content)

    def test_the_type_keeps_its_own_cover_not_another_types(self):
        self.assertEqual(resolve_template(self.srs).sources["cover_image"],
                         self.srs_cover)

    def test_the_region_travels_with_the_background(self):
        """x/y/width/height are drawn against a specific image. Taking the
        region from one template and the image from another puts the text in
        the wrong place on the page."""
        pages = resolve_template(self.invoice)
        self.assertEqual(
            (pages.content_x, pages.content_y,
             pages.content_width, pages.content_height),
            (10, 15, 80, 70))

    def test_it_has_visual_pages(self):
        self.assertTrue(resolve_template(self.invoice).has_visual_pages)


class SpecificityStillWins(TestCase):
    """Composition must not let a shared page override a type that has its own."""

    @classmethod
    def setUpTestData(cls):
        cls.contract = DocumentType.objects.create(name="Contract", slug="contract")
        cls.full = Template.objects.create(
            name="Contract full", document_type=cls.contract,
            cover_image=png("c-cover.png"),
            content_background=png("c-bg.png"),
            back_image=png("c-back.png"),
            content_x=5, content_y=5, content_width=90, content_height=90)
        cls.universal_content = Template.objects.create(
            name="Content Page", document_type=None,
            content_background=png("u-bg.png"))
        cls.universal_back = Template.objects.create(
            name="Thank You Page", document_type=None, back_image=png("u-back.png"))

    def test_a_complete_type_template_supplies_all_three(self):
        pages = resolve_template(self.contract)
        for field in ("cover_image", "content_background", "back_image"):
            with self.subTest(field=field):
                self.assertEqual(pages.sources[field], self.full)

    def test_and_keeps_its_own_region(self):
        pages = resolve_template(self.contract)
        self.assertEqual((pages.content_x, pages.content_width), (5, 90))

    def test_the_default_wins_among_a_types_templates(self):
        preferred = Template.objects.create(
            name="Contract preferred", document_type=self.contract,
            cover_image=png("pref.png"), is_default=True)
        self.assertEqual(
            resolve_template(self.contract).sources["cover_image"], preferred)

    def test_an_explicitly_chosen_template_leads(self):
        """A document pinned to one template uses its pages first, and still
        falls back for the slots it does not fill."""
        chosen = Template.objects.create(
            name="One-off", document_type=None, cover_image=png("one-off.png"))
        pages = resolve_template(self.contract, explicit=chosen)
        self.assertEqual(pages.sources["cover_image"], chosen)
        # Not filled by `chosen`, so the type's own template still supplies it.
        self.assertEqual(pages.sources["content_background"], self.full)


class NothingDesignedYet(TestCase):
    def test_an_empty_install_has_no_visual_pages(self):
        doc_type = DocumentType.objects.create(name="Brief", slug="brief")
        pages = resolve_template(doc_type)
        self.assertFalse(pages.has_visual_pages)
        self.assertFalse(pages)

    def test_it_falls_back_to_the_model_defaults_for_the_region(self):
        doc_type = DocumentType.objects.create(name="Brief2", slug="brief2")
        pages = resolve_template(doc_type)
        self.assertEqual((pages.content_x, pages.content_y,
                          pages.content_width, pages.content_height),
                         (8.0, 12.0, 84.0, 76.0))

    def test_a_type_with_no_templates_still_gets_the_shared_ones(self):
        doc_type = DocumentType.objects.create(name="Brief3", slug="brief3")
        shared = Template.objects.create(
            name="Content Page", document_type=None,
            content_background=png("shared-bg.png"))
        self.assertEqual(resolve_template(doc_type).sources["content_background"],
                         shared)


class ThePdfPipelineUsesTheComposedSet(TestCase):
    """`pdf.template_for` is the seam. If it ever goes back to returning a
    single Template, the universal pages silently stop printing again."""

    def test_template_for_composes_rather_than_choosing(self):
        from .models import Document
        from .pdf import template_for
        from clients.models import Client
        from projects.models import Project

        doc_type = DocumentType.objects.create(name="Invoice", slug="invoice")
        cover = Template.objects.create(
            name="Invoice", document_type=doc_type, cover_image=png("cov.png"))
        back = Template.objects.create(
            name="Thank You Page", document_type=None, back_image=png("bk.png"))

        client = Client.objects.create(name="Acme")
        project = Project.objects.create(client=client, name="Job")
        document = Document.objects.create(
            project=project, document_type=doc_type, title="INV-1",
            template=cover)

        pages = template_for(document)
        self.assertTrue(pages.cover_image)
        self.assertTrue(pages.back_image, "back page lost in the PDF pipeline")
        self.assertEqual(pages.sources["back_image"], back)
