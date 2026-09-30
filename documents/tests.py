import shutil
import tempfile
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from clients.models import Client
from .models import Document, GeneratedFile, Template


@override_settings(GROQ_API_KEYS=[])  # keep tests offline even when .env has keys
class DocumentEngineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_doctypes", verbosity=0)
        # role="OWNER" maps to the Super Admin RBAC role via the legacy shim.
        # It used to be the field's default, so these tests passed without
        # naming it — which was the bug: every user became a super admin.
        cls.user = get_user_model().objects.create_user(
            username="tester", password="pass12345", role="OWNER"
        )
        cls.client_obj = Client.objects.create(name="Testco", city="Hyderabad")
        cls.project = cls.client_obj.projects.first()  # default-project signal

    def setUp(self):
        self.client.force_login(self.user)

    def _create(self, type_slug, title, brief="A short brief."):
        return self.client.post(
            reverse("documents:create", args=[self.project.pk, type_slug]),
            {"title": title, "brief": brief},
        )

    def test_default_project_signal(self):
        self.assertIsNotNone(self.project)
        self.assertTrue(self.project.is_default)

    def test_every_type_is_driven_by_one_brief(self):
        """The brief is still the single input for every type. Priced types add
        the few invoice facts a brief can't carry (dates, PO ref) — but never a
        money field: amounts live in line items, not in the creation form."""
        from .models import DocumentType
        for dt in DocumentType.objects.all():
            names = [f["name"] for f in dt.form_schema]
            self.assertEqual(names[0], "brief")
            self.assertIn("{brief}", dt.ai_prompt)
            if dt.is_priced:
                self.assertEqual(names, ["brief", "issue_date", "due_date", "po_number"])
            else:
                self.assertEqual(names, ["brief"])

    def test_generate_edit_version_flow(self):
        self._create("proposal", "P1", brief="Site for lead generation, 6 weeks.")
        doc = Document.objects.get(title="P1")
        self.assertEqual(doc.current_version.version_number, 1)
        self.assertEqual(doc.form_data, {"brief": "Site for lead generation, 6 weeks."})
        self.client.post(reverse("documents:save", args=[doc.pk]),
                         {"content_html": "<p>edit</p>"})
        doc.refresh_from_db()
        self.assertEqual(doc.current_version.version_number, 2)
        self.client.post(reverse("documents:regenerate", args=[doc.pk]))
        doc.refresh_from_db()
        self.assertEqual(doc.current_version.version_number, 3)

    def test_invoice_and_quotation_numbering(self):
        self._create("invoice", "I1", brief="Design work, 1,00,000 INR + 18% GST")
        self._create("invoice", "I2", brief="Dev work, 50,000 INR")
        self._create("quotation", "Q1", brief="Design - 30000, 50/50 terms")
        i1 = Document.objects.get(title="I1")
        i2 = Document.objects.get(title="I2")
        year = i1.created_at.year
        self.assertEqual(i1.number, f"INV-{year}-001")
        self.assertEqual(i2.number, f"INV-{year}-002")
        self.assertEqual(Document.objects.get(title="Q1").number, f"QUO-{year}-001")
        # A priced document's body is rendered from its line items, so the
        # number printed on it comes from the server, not from the AI's prose.
        self.assertIn(i1.number, i1.current_version.content_html)
        self.assertNotIn(i2.number, i1.current_version.content_html)

    def test_the_brief_reaches_the_ai_prompt(self):
        """Offline, the placeholder echoes the prompt it would have sent — which
        is how we check the brief is actually being substituted in."""
        self._create("proposal", "P-brief", brief="Site for 1,00,000 INR")
        content = Document.objects.get(title="P-brief").current_version.content_html
        self.assertIn("1,00,000", content)

    def test_status_flow_enforced(self):
        self._create("proposal", "S1")
        doc = Document.objects.get(title="S1")
        url = reverse("documents:set_status", args=[doc.pk])
        self.client.post(url, {"status": "APPROVED"})  # illegal jump from DRAFT
        doc.refresh_from_db()
        self.assertEqual(doc.status, "DRAFT")
        for step in ("REVIEW", "APPROVED", "SENT"):
            self.client.post(url, {"status": step})
        doc.refresh_from_db()
        self.assertEqual(doc.status, "SENT")

    def test_autosave_reuses_version(self):
        self._create("proposal", "A1")
        doc = Document.objects.get(title="A1")
        url = reverse("documents:autosave", args=[doc.pk])
        self.client.post(url, {"content_html": "<p>d1</p>"})
        self.client.post(url, {"content_html": "<p>d2</p>"})
        doc.refresh_from_db()
        self.assertEqual(doc.versions.count(), 2)  # AI draft + one autosave
        self.assertEqual(doc.current_version.content_html, "<p>d2</p>")
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_pdf_export(self):
        self._create("invoice", "E1", brief="Design work, 10,000 INR")
        doc = Document.objects.get(title="E1")
        r = self.client.post(reverse("documents:export_pdf", args=[doc.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF-"))
        exported = GeneratedFile.objects.get(document=doc)
        self.assertEqual(exported.version, doc.current_version)


@override_settings(GROQ_API_KEYS=[])  # keep tests offline even when .env has keys
class DocumentLibraryTests(TestCase):
    """M6: duplicate, archive/restore, version compare/restore. M7: role gates."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_doctypes", verbosity=0)
        User = get_user_model()
        cls.owner = User.objects.create_user("owner", password="x", role="OWNER")
        cls.dev = User.objects.create_user("dev", password="x", role="DEV")
        cls.acct = User.objects.create_user("acct", password="x", role="ACCT")
        cls.client_obj = Client.objects.create(name="LibCo")
        cls.project = cls.client_obj.projects.first()
        # Documents inherit their project's visibility, so the non-manager
        # roles under test have to be on the project to reach them at all.
        cls.project.members.add(cls.owner, cls.dev, cls.acct)

    def _make_doc(self, title="D1"):
        self.client.force_login(self.owner)
        self.client.post(reverse("documents:create", args=[self.project.pk, "invoice"]),
                         {"title": title, "brief": "Dev work, 10,000 INR + 18% GST"})
        return Document.objects.get(title=title)

    def test_duplicate_gets_a_fresh_number_and_reprints_it(self):
        """An invoice's body prints its own number, so a duplicate cannot just
        reuse the source's HTML — it would ship an invoice claiming a number
        that belongs to a different document."""
        doc = self._make_doc()
        self.client.post(reverse("documents:duplicate", args=[doc.pk]))
        dup = Document.objects.get(title="D1 (copy)")
        self.assertNotEqual(dup.number, doc.number)
        self.assertTrue(dup.number.startswith("INV-"))
        self.assertIn(dup.number, dup.current_version.content_html)
        self.assertNotIn(doc.number, dup.current_version.content_html)

    def test_duplicating_an_unpriced_document_copies_its_content_verbatim(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("documents:create", args=[self.project.pk, "proposal"]),
                         {"title": "Prop", "brief": "A brief."})
        doc = Document.objects.get(title="Prop")
        self.client.post(reverse("documents:duplicate", args=[doc.pk]))
        dup = Document.objects.get(title="Prop (copy)")
        self.assertEqual(dup.current_version.content_html,
                         doc.current_version.content_html)

    def test_archive_hides_and_restore_returns(self):
        doc = self._make_doc()
        self.client.post(reverse("documents:archive", args=[doc.pk]))
        doc.refresh_from_db()
        self.assertTrue(doc.is_archived)
        body = self.client.get(reverse("documents:list")).content.decode()
        self.assertNotIn("<strong>D1</strong>", body)
        archived = self.client.get(reverse("documents:list") + "?archived=1")
        self.assertIn("<strong>D1</strong>", archived.content.decode())
        self.client.post(reverse("documents:unarchive", args=[doc.pk]))
        doc.refresh_from_db()
        self.assertFalse(doc.is_archived)

    def test_restore_version_keeps_history(self):
        doc = self._make_doc()
        self.client.post(reverse("documents:save", args=[doc.pk]),
                         {"content_html": "<p>v2</p>"})
        doc.refresh_from_db()
        v1_content = doc.versions.get(version_number=1).content_html
        self.client.post(reverse("documents:restore_version", args=[doc.pk, 1]))
        doc.refresh_from_db()
        self.assertEqual(doc.current_version.version_number, 3)
        self.assertEqual(doc.current_version.content_html, v1_content)
        self.assertEqual(doc.versions.count(), 3)

    def test_compare_page_renders_diff(self):
        doc = self._make_doc()
        self.client.post(reverse("documents:save", args=[doc.pk]),
                         {"content_html": "<p>changed line</p>"})
        r = self.client.get(reverse("documents:compare", args=[doc.pk]) + "?a=1&b=2")
        self.assertEqual(r.status_code, 200)
        self.assertIn("diff", r.content.decode())

    def test_developer_role_is_read_only(self):
        invoice = self._make_doc()  # a financial document
        # a non-financial document the developer is allowed to read
        self.client.force_login(self.owner)
        self.client.post(reverse("documents:create", args=[self.project.pk, "contract"]),
                         {"title": "C1", "brief": "Service terms."})
        contract = Document.objects.get(title="C1")

        self.client.force_login(self.dev)
        # can't create anything (no documents.create)
        self.client.post(reverse("documents:create", args=[self.project.pk, "srs"]),
                         {"title": "Nope", "brief": "x"})
        self.assertFalse(Document.objects.filter(title="Nope").exists())
        # read-only on a normal document: may view, may not autosave
        self.assertEqual(self.client.get(contract.get_absolute_url()).status_code, 200)
        r = self.client.post(reverse("documents:autosave", args=[contract.pk]),
                             {"content_html": "<p>hack</p>"})
        self.assertEqual(r.status_code, 403)
        # finance documents are blocked outright — not even read access
        self.assertEqual(
            self.client.get(invoice.get_absolute_url()).status_code, 302)

    def test_accounts_role_cannot_approve(self):
        doc = self._make_doc()
        url = reverse("documents:set_status", args=[doc.pk])
        self.client.post(url, {"status": "REVIEW"})  # owner submits
        self.client.force_login(self.acct)
        self.client.post(url, {"status": "APPROVED"})
        doc.refresh_from_db()
        self.assertEqual(doc.status, "REVIEW")

    def test_review_submission_notifies_approvers(self):
        from core.models import Notification
        doc = self._make_doc()
        self.client.force_login(self.acct)
        self.client.post(reverse("documents:set_status", args=[doc.pk]),
                         {"status": "REVIEW"})
        self.assertTrue(Notification.objects.filter(
            user=self.owner, is_read=False).exists())
        self.assertFalse(Notification.objects.filter(user=self.acct).exists())


_MEDIA_TMP = tempfile.mkdtemp(prefix="dv-test-media-")


def _png(name):
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (105, 148), "white").save(buf, "PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


@override_settings(MEDIA_ROOT=_MEDIA_TMP)
class TemplateDesignerTests(TestCase):
    """The visual template designer: images + one drawn content region."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_doctypes", verbosity=0)
        User = get_user_model()
        cls.owner = User.objects.create_user("owner", password="x", role="OWNER")
        cls.dev = User.objects.create_user("dev", password="x", role="DEV")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_TMP, ignore_errors=True)

    def test_designer_saves_drawn_region_and_images(self):
        self.client.force_login(self.owner)
        r = self.client.post(reverse("doc_templates:create"), {
            "name": "Brand 2026",
            "document_type": "",
            "content_x": "7.25", "content_y": "13.5",
            "content_width": "85.5", "content_height": "72.75",
            "is_default": "on",
            "cover_image": _png("cover.png"),
            "content_background": _png("content.png"),
            "back_image": _png("back.png"),
        })
        self.assertRedirects(r, reverse("doc_templates:list"))
        t = Template.objects.get(name="Brand 2026")
        self.assertEqual((t.content_x, t.content_y), (7.25, 13.5))
        self.assertEqual((t.content_width, t.content_height), (85.5, 72.75))
        self.assertTrue(t.is_default)
        self.assertTrue(t.cover_image.name.startswith("templates/covers/"))
        self.assertTrue(t.content_background.name.startswith("templates/content/"))
        self.assertTrue(t.back_image.name.startswith("templates/backs/"))

    def test_region_is_clamped_to_the_page(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("doc_templates:create"), {
            "name": "Wild region",
            "content_x": "90", "content_y": "-5",
            "content_width": "50", "content_height": "junk",
        })
        t = Template.objects.get(name="Wild region")
        self.assertLessEqual(t.content_x + t.content_width, 100)
        self.assertGreaterEqual(t.content_y, 0)
        self.assertGreaterEqual(t.content_height, 1)

    def test_only_one_default_per_document_type(self):
        self.client.force_login(self.owner)
        for name in ("First", "Second"):
            self.client.post(reverse("doc_templates:create"), {
                "name": name, "content_x": "8", "content_y": "12",
                "content_width": "84", "content_height": "76", "is_default": "on",
            })
        defaults = Template.objects.filter(document_type=None, is_default=True)
        self.assertEqual(list(defaults.values_list("name", flat=True)), ["Second"])

    def test_edit_updates_region_without_reuploading_images(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("doc_templates:create"), {
            "name": "Editable", "content_x": "8", "content_y": "12",
            "content_width": "84", "content_height": "76",
            "cover_image": _png("cover.png"),
        })
        t = Template.objects.get(name="Editable")
        old_cover = t.cover_image.name
        self.client.post(reverse("doc_templates:edit", args=[t.pk]), {
            "name": "Editable", "content_x": "10", "content_y": "20",
            "content_width": "80", "content_height": "60",
        })
        t.refresh_from_db()
        self.assertEqual((t.content_x, t.content_y), (10.0, 20.0))
        self.assertEqual(t.cover_image.name, old_cover)  # kept, not cleared

    def test_content_only_template_needs_no_cover(self):
        """The constant-content-page case: one background, no cover, no back.
        Nothing in the designer should insist on the other two pages."""
        self.client.force_login(self.owner)
        r = self.client.post(reverse("doc_templates:create"), {
            "name": "Constant content", "document_type": "",
            "content_x": "8", "content_y": "12",
            "content_width": "84", "content_height": "76",
            "is_default": "on",
            "content_background": _png("content.png"),
        })
        self.assertRedirects(r, reverse("doc_templates:list"))
        t = Template.objects.get(name="Constant content")
        self.assertTrue(t.content_background)
        self.assertFalse(t.cover_image)
        self.assertFalse(t.back_image)
        self.assertTrue(t.has_visual_pages)   # still drives the image renderer
        # and it is the any-type fallback every document reaches for
        self.assertIsNone(t.document_type)
        self.assertTrue(t.is_default)

    def test_pages_can_be_removed_after_upload(self):
        """Turning an existing cover+content template into a content-only one."""
        self.client.force_login(self.owner)
        self.client.post(reverse("doc_templates:create"), {
            "name": "Trimmable", "content_x": "8", "content_y": "12",
            "content_width": "84", "content_height": "76",
            "cover_image": _png("cover.png"),
            "content_background": _png("content.png"),
        })
        t = Template.objects.get(name="Trimmable")
        self.assertTrue(t.cover_image)
        kept = t.content_background.name
        self.client.post(reverse("doc_templates:edit", args=[t.pk]), {
            "name": "Trimmable", "content_x": "8", "content_y": "12",
            "content_width": "84", "content_height": "76",
            "remove_cover_image": "on",
        })
        t.refresh_from_db()
        self.assertFalse(t.cover_image)
        self.assertEqual(t.content_background.name, kept)  # untouched

    def test_designer_writes_are_owner_only(self):
        self.client.force_login(self.dev)
        self.assertEqual(
            self.client.get(reverse("doc_templates:list")).status_code, 200)
        self.client.post(reverse("doc_templates:create"), {
            "name": "Not allowed", "content_x": "8", "content_y": "12",
            "content_width": "84", "content_height": "76",
        })
        self.assertFalse(Template.objects.filter(name="Not allowed").exists())


@override_settings(GROQ_API_KEYS=[], MEDIA_ROOT=_MEDIA_TMP)
class VisualPdfExportTests(TestCase):
    """End-to-end: template images drive the exported PDF's pages."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_doctypes", verbosity=0)
        cls.owner = get_user_model().objects.create_user(
            "owner", password="x", role="OWNER")
        cls.client_obj = Client.objects.create(name="PdfCo")
        cls.project = cls.client_obj.projects.first()
        cls.template = Template.objects.create(
            name="Brand", is_default=True,
            cover_image=_png("cover.png"),
            content_background=_png("content.png"),
            back_image=_png("back.png"),
            content_x=8, content_y=12, content_width=84, content_height=76,
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_TMP, ignore_errors=True)

    def _pages(self, pdf_bytes):
        from pypdf import PdfReader
        return PdfReader(BytesIO(pdf_bytes)).pages

    def _export(self, title):
        self.client.force_login(self.owner)
        self.client.post(reverse("documents:create", args=[self.project.pk, "proposal"]),
                         {"title": title, "brief": "A site."})
        doc = Document.objects.get(title=title)
        r = self.client.post(reverse("documents:export_pdf", args=[doc.pk]))
        self.assertEqual(r.status_code, 200)
        return doc, r.content

    def test_new_document_gets_the_default_template(self):
        doc, _ = self._export("T1")
        self.assertEqual(doc.template, self.template)

    def test_pdf_has_cover_content_and_back_pages(self):
        _, pdf = self._export("T2")
        pages = self._pages(pdf)
        # page 1 cover + at least one content page + constant last page
        self.assertGreaterEqual(len(pages), 3)
        # cover and back carry only their full-bleed image, no text
        self.assertEqual(pages[0].extract_text().strip(), "")
        self.assertEqual(pages[-1].extract_text().strip(), "")
        self.assertGreaterEqual(len(pages[0].images), 1)   # cover image rendered
        self.assertGreaterEqual(len(pages[-1].images), 1)  # back image rendered
        self.assertIn("Proposal", "".join(p.extract_text() for p in pages[1:-1]))

    def test_document_without_template_uses_branded_shell(self):
        self.template.delete()
        _, pdf = self._export("T3")
        pages = self._pages(pdf)
        self.assertGreaterEqual(len(pages), 1)
        self.assertIn("Livo Digital", pages[0].extract_text())


@override_settings(GROQ_API_KEYS=[])
class MonthlyReportTests(TestCase):
    """Offline-mode monthly report: facts draft + injected proof images."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.mkdtemp()
        cls._media_override = override_settings(MEDIA_ROOT=cls._media)
        cls._media_override.enable()
        cls.addClassCleanup(cls._media_override.disable)
        cls.addClassCleanup(shutil.rmtree, cls._media, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        call_command("seed_doctypes", verbosity=0)
        User = get_user_model()
        cls.pm = User.objects.create_user("pm-rep", password="x", role="PM")
        cls.sales = User.objects.create_user("sales-rep", password="x", role="SALES")
        cls.client_obj = Client.objects.create(name="Reportco")
        cls.project = cls.client_obj.projects.first()
        cls.project.members.add(cls.pm, cls.sales)

    def _seed_month_data(self):
        from django.utils import timezone
        from projects.models import Milestone
        today = timezone.localdate()
        self.project.work_logs.create(
            date=today, category="DEV", description="Built checkout flow", hours="4")
        self.project.work_logs.create(
            date=today, category="DESIGN", description="Redesigned hero banner")
        self.project.tasks.create(title="Ship cart page", status="DONE",
                                  completed_on=today)
        self.project.milestones.create(title="Beta launch",
                                       status=Milestone.Status.DONE)
        return self.project.assets.create(
            title="Checkout screenshot", file=_png("proof.png"),
            category="PROOF", is_report_proof=True)

    def test_service_builds_facts_and_injects_images(self):
        from django.utils import timezone
        from .services import generate_monthly_report
        self._seed_month_data()
        today = timezone.localdate()
        html = generate_monthly_report(self.project, today.year, today.month,
                                       highlights="Beta went live")
        self.assertIn("Built checkout flow", html)
        self.assertIn("Ship cart page", html)
        self.assertIn("Beta launch", html)
        self.assertIn("Beta went live", html)
        self.assertIn("Proof &amp; Impact", html)
        self.assertIn('<img src="/media/project_assets/', html)
        self.assertIn("Checkout screenshot", html)

    def test_explicit_empty_selection_skips_images(self):
        from django.utils import timezone
        from .services import generate_monthly_report
        self._seed_month_data()
        today = timezone.localdate()
        html = generate_monthly_report(self.project, today.year, today.month,
                                       selected_asset_ids=[])
        self.assertNotIn("Proof", html)
        self.assertNotIn("<img", html)

    def test_view_creates_editable_document(self):
        from django.utils import timezone
        asset = self._seed_month_data()
        self.client.force_login(self.pm)
        month = timezone.localdate().strftime("%Y-%m")
        r = self.client.post(
            reverse("documents:monthly_report", args=[self.project.pk]),
            {"month": month, "highlights": "Great month",
             "assets": [str(asset.pk)]},
        )
        doc = Document.objects.get(document_type__slug="monthly-report")
        self.assertRedirects(r, doc.get_absolute_url())
        self.assertIn("Monthly Report –", doc.title)
        self.assertIn(self.client_obj.name, doc.title)
        self.assertEqual(doc.current_version.note, "AI generated")
        self.assertIn("<img", doc.current_version.content_html)

    def test_sales_cannot_generate(self):
        self.client.force_login(self.sales)
        r = self.client.post(
            reverse("documents:monthly_report", args=[self.project.pk]), {})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(
            Document.objects.filter(document_type__slug="monthly-report").exists())

    def test_exported_pdf_contains_the_photo(self):
        from django.utils import timezone
        from .pdf import render_document_pdf
        from .services import generate_monthly_report
        from .models import DocumentType
        self._seed_month_data()
        today = timezone.localdate()
        html = generate_monthly_report(self.project, today.year, today.month)
        doc = Document.objects.create(
            project=self.project,
            document_type=DocumentType.objects.get(slug="monthly-report"),
            title="Report", created_by=self.pm,
        )
        from .models import DocumentVersion
        version = DocumentVersion.objects.create(
            document=doc, version_number=1, content_html=html)
        doc.current_version = version
        doc.save(update_fields=["current_version"])
        pdf = render_document_pdf(doc)
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(pdf))
        embedded = sum(len(page.images) for page in reader.pages)
        self.assertGreaterEqual(embedded, 1, "photo missing from exported PDF")
