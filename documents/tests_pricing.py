"""
Priced documents: line items, server-side totals, and the editor that drives them.

The property under test throughout is that **no figure a client sees comes from
the AI**. The model proposes rows; `documents.pricing` computes every amount;
the body is re-rendered from those same rows. These tests pin each link in
that chain, including the failure path where the AI is unreachable.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from clients.models import Client

from .models import Document, DocumentType, LineItem
from .pricing import (
    amount_in_words, format_amount, resolve_gst_kind, rows_from_ai, rows_from_post,
    totals,
)


class _Row:
    """A stand-in with the three properties `totals` reads, so the pure math
    can be tested without touching the database."""

    def __init__(self, amount, tax_rate):
        self.amount = Decimal(amount)
        self.tax_rate = Decimal(tax_rate)

    @property
    def tax_amount(self):
        return (self.amount * self.tax_rate / 100).quantize(Decimal("0.01"))


class MoneyFormattingTests(TestCase):
    def test_inr_uses_indian_digit_grouping(self):
        self.assertEqual(format_amount(Decimal("1234567.5"), "INR"), "₹12,34,567.50")
        self.assertEqual(format_amount(Decimal("123456"), "INR"), "₹1,23,456.00")
        self.assertEqual(format_amount(Decimal("1234"), "INR"), "₹1,234.00")
        self.assertEqual(format_amount(Decimal("999"), "INR"), "₹999.00")

    def test_other_currencies_use_thousands_grouping(self):
        self.assertEqual(format_amount(Decimal("1234567.5"), "USD"), "$1,234,567.50")

    def test_negative_amounts_keep_the_sign_outside_the_symbol(self):
        self.assertEqual(format_amount(Decimal("-5000"), "INR"), "-₹5,000.00")

    def test_amount_in_words_uses_the_indian_scale(self):
        self.assertEqual(amount_in_words(Decimal("120000")),
                         "Rupees One Lakh Twenty Thousand Only")
        self.assertEqual(amount_in_words(Decimal("100.50")),
                         "Rupees One Hundred and Fifty Paise Only")
        self.assertEqual(amount_in_words(Decimal("0")), "Rupees Zero Only")

    def test_amount_in_words_is_empty_for_currencies_we_dont_spell(self):
        self.assertEqual(amount_in_words(Decimal("100"), "USD"), "")


class TotalsTests(TestCase):
    def test_subtotal_tax_and_grand_total(self):
        result = totals([_Row("50000", "18"), _Row("25000", "18")])
        self.assertEqual(result["subtotal"], Decimal("75000.00"))
        self.assertEqual(result["tax_total"], Decimal("13500.00"))
        self.assertEqual(result["grand_total"], Decimal("88500.00"))

    def test_mixed_rates_are_reported_separately(self):
        """A zero-rated reimbursement alongside an 18% service line has to show
        as two tax rows, not one blended rate nobody can reconcile."""
        result = totals([_Row("10000", "18"), _Row("2000", "0")])
        rates = [group["rate_display"] for group in result["tax_groups"]]
        self.assertEqual(rates, ["0", "18"])
        self.assertEqual(result["tax_total"], Decimal("1800.00"))
        self.assertEqual(result["grand_total"], Decimal("13800.00"))

    def test_a_negative_line_discounts_the_total(self):
        result = totals([_Row("10000", "18"), _Row("-1000", "18")])
        self.assertEqual(result["subtotal"], Decimal("9000.00"))
        self.assertEqual(result["grand_total"], Decimal("10620.00"))

    def test_empty_document_totals_to_zero(self):
        result = totals([])
        self.assertEqual(result["grand_total"], Decimal("0.00"))
        self.assertEqual(result["tax_groups"], [])


class GstModeTests(TestCase):
    """Whether GST is charged at all, and how it is broken up."""

    class _Party:
        def __init__(self, state):
            self.state = state

    def test_automatic_splits_within_a_state_and_charges_igst_across(self):
        telangana, karnataka = self._Party("Telangana"), self._Party("Karnataka")
        self.assertEqual(resolve_gst_kind("auto", telangana, telangana), "split")
        self.assertEqual(resolve_gst_kind("auto", telangana, karnataka), "igst")
        # case and stray spacing must not change the tax treatment
        self.assertEqual(
            resolve_gst_kind("auto", telangana, self._Party("  telangana ")), "split")

    def test_automatic_refuses_to_guess_when_a_state_is_missing(self):
        """A wrongly split invoice is worse than an unsplit one, so an unknown
        state falls back to a single combined GST line."""
        self.assertEqual(
            resolve_gst_kind("auto", self._Party(""), self._Party("Goa")), "gst")
        self.assertEqual(
            resolve_gst_kind("auto", self._Party("Goa"), self._Party("")), "gst")

    def test_explicit_modes_override_the_states(self):
        same = self._Party("Kerala")
        self.assertEqual(resolve_gst_kind("igst", same, same), "igst")
        self.assertEqual(
            resolve_gst_kind("cgst_sgst", same, self._Party("Bihar")), "split")
        self.assertEqual(resolve_gst_kind("single", same, same), "gst")

    def test_not_charging_gst_zeroes_the_tax_without_touching_the_rows(self):
        rows = [_Row("50000", "18")]
        charged = totals(rows, charge_gst=True)
        exempt = totals(rows, charge_gst=False)
        self.assertEqual(exempt["tax_total"], Decimal("0.00"))
        self.assertEqual(exempt["grand_total"], exempt["subtotal"])
        self.assertEqual(exempt["tax_rows"], [])
        # the rate survives on the row, so ticking the box back on restores it
        self.assertEqual(rows[0].tax_rate, Decimal("18"))
        self.assertEqual(charged["grand_total"], Decimal("59000.00"))

    def test_split_produces_half_rate_cgst_and_sgst_rows(self):
        result = totals([_Row("50000", "18")], gst_kind="split")
        self.assertEqual([row["label"] for row in result["tax_rows"]],
                         ["CGST @ 9%", "SGST @ 9%"])
        self.assertEqual([row["amount"] for row in result["tax_rows"]],
                         [Decimal("4500.00"), Decimal("4500.00")])
        self.assertEqual(result["grand_total"], Decimal("59000.00"))

    def test_the_two_halves_always_add_back_to_the_gst_charged(self):
        """An odd paisa cannot go missing between CGST and SGST — the invoice
        has to reconcile against itself."""
        result = totals([_Row("4501.50", "18")], gst_kind="split")
        halves = [row["amount"] for row in result["tax_rows"]]
        self.assertEqual(halves, [Decimal("405.14"), Decimal("405.13")])
        self.assertEqual(sum(halves), result["tax_total"])
        self.assertEqual(result["tax_total"], Decimal("810.27"))

    def test_igst_is_one_full_rate_row(self):
        result = totals([_Row("50000", "18")], gst_kind="igst")
        self.assertEqual([row["label"] for row in result["tax_rows"]], ["IGST @ 18%"])
        self.assertEqual(result["tax_total"], Decimal("9000.00"))

    def test_the_split_never_changes_what_is_owed(self):
        rows = [_Row("4501.50", "18"), _Row("1234.56", "5")]
        self.assertEqual(totals(rows, gst_kind="split")["grand_total"],
                         totals(rows, gst_kind="igst")["grand_total"])


class RowCleaningTests(TestCase):
    def test_ai_rows_are_cleaned_like_any_untrusted_input(self):
        rows = rows_from_ai([
            {"description": "Landing page", "quantity": "2", "unit_price": "25,000",
             "tax_rate": 18, "total": 59000, "subtotal": "nonsense"},
            {"description": "", "unit_price": 0},          # empty slot, dropped
            "not a dict",                                   # junk, dropped
            {"description": "Hosting", "unit_price": "9999999999999", "tax_rate": "900"},
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["description"], "Landing page")
        self.assertEqual(rows[0]["unit_price"], Decimal("25000.00"))  # comma stripped
        self.assertNotIn("total", rows[0])          # AI arithmetic never survives
        self.assertEqual(rows[1]["tax_rate"], Decimal("100.00"))      # clamped
        self.assertLessEqual(rows[1]["unit_price"], Decimal("9999999999.99"))

    def test_posted_rows_drop_blank_slots_and_keep_order(self):
        from django.http import QueryDict

        post = QueryDict(mutable=True)
        post.setlist("item_description", ["Design", "", "Build"])
        post.setlist("item_quantity", ["2", "1", "1"])
        post.setlist("item_unit", ["pages", "", ""])
        post.setlist("item_unit_price", ["1000", "", "5000"])
        post.setlist("item_tax_rate", ["18", "18", "5"])
        rows = rows_from_post(post)
        self.assertEqual([r["description"] for r in rows], ["Design", "Build"])
        self.assertEqual([r["order"] for r in rows], [0, 1])
        self.assertEqual(rows[1]["tax_rate"], Decimal("5.00"))


@override_settings(GROQ_API_KEYS=[])   # every test here runs with the AI offline
class InvoiceDocumentTests(TestCase):
    """The invoice as a user meets it: create, edit the table, delete a row."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_doctypes", verbosity=0)
        cls.owner = get_user_model().objects.create_user(
            "owner", password="x", role="OWNER")
        cls.client_obj = Client.objects.create(name="Testco", city="Hyderabad")
        cls.project = cls.client_obj.projects.first()
        cls.invoice_type = DocumentType.objects.get(slug="invoice")

    def setUp(self):
        self.client.force_login(self.owner)

    def _create_invoice(self):
        self.client.post(
            reverse("documents:create", args=[self.project.pk, "invoice"]),
            {"title": "Invoice for Testco", "brief": "Two landing pages at 25000 each."})
        return Document.objects.get(title="Invoice for Testco")

    def _save_items(self, document, rows, **meta):
        payload = {
            "item_description": [r[0] for r in rows],
            "item_quantity": [r[1] for r in rows],
            "item_unit": [r[2] for r in rows],
            "item_unit_price": [r[3] for r in rows],
            "item_tax_rate": [r[4] for r in rows],
        }
        payload.setdefault("charge_gst", "on")     # the editor's default state
        payload.update(meta)
        # None means "this field was not posted at all" — an unticked checkbox.
        payload = {k: v for k, v in payload.items() if v is not None}
        return self.client.post(
            reverse("documents:save_items", args=[document.pk]), payload)

    def test_invoice_type_is_priced_and_others_are_not(self):
        self.assertTrue(self.invoice_type.is_priced)
        self.assertTrue(DocumentType.objects.get(slug="quotation").is_priced)
        self.assertFalse(DocumentType.objects.get(slug="proposal").is_priced)

    def test_creating_an_invoice_offline_still_produces_a_usable_draft(self):
        """No API key is the normal state on a dev box and a real outage in
        production — the invoice must still exist, numbered and editable."""
        document = self._create_invoice()
        self.assertTrue(document.number.startswith("INV-"))
        self.assertIsNotNone(document.current_version)
        self.assertIn("TAX INVOICE", document.current_version.content_html)
        self.assertIn(document.number, document.current_version.content_html)
        # today's date is filled in so the invoice is not dateless
        self.assertTrue(document.form_data["issue_date"])

    def test_saving_line_items_rebuilds_the_body_with_computed_totals(self):
        document = self._create_invoice()
        self._save_items(document, [
            ("Landing page design", "2", "pages", "25000", "18"),
            ("Annual hosting", "1", "yr", "12000", "18"),
        ], notes="50% advance.", issue_date="2026-08-19", due_date="2026-09-03")

        document.refresh_from_db()
        self.assertEqual(document.line_items.count(), 2)
        html = document.current_version.content_html
        # 62000 + 18% = 73160 — computed here, never parsed out of AI prose
        self.assertIn("₹73,160.00", html)
        self.assertIn("₹62,000.00", html)
        self.assertIn("₹11,160.00", html)
        self.assertIn("Rupees Seventy Three Thousand One Hundred Sixty Only", html)
        self.assertIn("50% advance.", html)
        self.assertIn("03 Sep 2026", html)

    def test_deleting_a_row_is_simply_not_posting_it(self):
        document = self._create_invoice()
        self._save_items(document, [
            ("Design", "1", "", "10000", "18"),
            ("Build", "1", "", "20000", "18"),
        ])
        self.assertEqual(document.line_items.count(), 2)

        self._save_items(document, [("Design", "1", "", "10000", "18")])
        document.refresh_from_db()
        self.assertEqual(
            list(document.line_items.values_list("description", flat=True)), ["Design"])
        self.assertIn("₹11,800.00", document.current_version.content_html)

    def test_clearing_every_row_leaves_an_empty_but_valid_invoice(self):
        document = self._create_invoice()
        self._save_items(document, [("Design", "1", "", "10000", "18")])
        self.client.post(reverse("documents:save_items", args=[document.pk]), {})
        document.refresh_from_db()
        self.assertEqual(document.line_items.count(), 0)
        self.assertIn("No line items yet", document.current_version.content_html)

    def test_each_save_is_one_new_version(self):
        document = self._create_invoice()
        before = document.versions.count()
        self._save_items(document, [("Design", "1", "", "10000", "18")])
        document.refresh_from_db()
        self.assertEqual(document.versions.count(), before + 1)
        self.assertEqual(document.current_version.note, "Line items updated")

    def test_editing_an_approved_invoice_sends_it_back_to_draft(self):
        document = self._create_invoice()
        document.status = Document.Status.APPROVED
        document.save(update_fields=["status"])
        self._save_items(document, [("Design", "1", "", "10000", "18")])
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.DRAFT)

    def test_duplicating_an_invoice_copies_rows_and_renumbers_the_body(self):
        document = self._create_invoice()
        self._save_items(document, [("Design", "1", "", "10000", "18")])
        self.client.post(reverse("documents:duplicate", args=[document.pk]))

        copy = Document.objects.get(title="Invoice for Testco (copy)")
        self.assertEqual(copy.line_items.count(), 1)
        self.assertNotEqual(copy.number, document.number)
        # the body must print the COPY's number, not the original's
        self.assertIn(copy.number, copy.current_version.content_html)
        self.assertNotIn(document.number, copy.current_version.content_html)

    def test_unticking_charge_gst_removes_it_from_the_exported_body(self):
        document = self._create_invoice()
        self._save_items(document, [("Design", "1", "", "10000", "18")])
        document.refresh_from_db()
        self.assertIn("11,800.00", document.current_version.content_html)

        # an unticked checkbox posts nothing at all
        self._save_items(document, [("Design", "1", "", "10000", "18")],
                         charge_gst=None)
        document.refresh_from_db()
        html = document.current_version.content_html
        self.assertIn("10,000.00", html)
        self.assertNotIn("11,800.00", html)
        self.assertIn("No GST charged", html)
        self.assertFalse(document.form_data["charge_gst"])

    def test_same_state_client_gets_cgst_and_sgst_automatically(self):
        from core.models import AgencySettings

        agency = AgencySettings.load()
        agency.state = "Telangana"
        agency.save(update_fields=["state"])
        self.client_obj.state = "Telangana"
        self.client_obj.save(update_fields=["state"])

        document = self._create_invoice()
        self._save_items(document, [("Design", "1", "", "10000", "18")],
                         gst_mode="auto")
        document.refresh_from_db()
        html = document.current_version.content_html
        self.assertIn("CGST @ 9%", html)
        self.assertIn("SGST @ 9%", html)
        self.assertNotIn("IGST", html)
        self.assertIn("11,800.00", html)   # the split changes nothing owed

    def test_other_state_client_gets_igst(self):
        from core.models import AgencySettings

        agency = AgencySettings.load()
        agency.state = "Telangana"
        agency.save(update_fields=["state"])
        self.client_obj.state = "Karnataka"
        self.client_obj.save(update_fields=["state"])

        document = self._create_invoice()
        self._save_items(document, [("Design", "1", "", "10000", "18")],
                         gst_mode="auto")
        document.refresh_from_db()
        html = document.current_version.content_html
        self.assertIn("IGST @ 18%", html)
        self.assertNotIn("CGST", html)

    def test_an_explicit_mode_beats_the_state_comparison(self):
        document = self._create_invoice()
        self._save_items(document, [("Design", "1", "", "10000", "18")],
                         gst_mode="cgst_sgst")
        document.refresh_from_db()
        self.assertEqual(document.form_data["gst_mode"], "cgst_sgst")
        self.assertIn("CGST @ 9%", document.current_version.content_html)

    def test_a_junk_gst_mode_falls_back_to_automatic(self):
        document = self._create_invoice()
        self._save_items(document, [("Design", "1", "", "10000", "18")],
                         gst_mode="../../etc/passwd")
        document.refresh_from_db()
        self.assertEqual(document.form_data["gst_mode"], "auto")

    def test_documents_predating_the_toggle_still_charge_gst(self):
        """No charge_gst key at all has to mean "charge it" — an invoice that
        silently stopped adding GST would be the worse default."""
        from .pricing import build_context

        document = self._create_invoice()
        document.form_data = {"issue_date": "2026-08-19"}   # how an old row looks
        document.save(update_fields=["form_data"])
        LineItem.objects.create(document=document, description="Design",
                                quantity=Decimal("1"), unit_price=Decimal("10000"),
                                tax_rate=Decimal("18"))
        context = build_context(document)
        self.assertTrue(context["charge_gst"])
        self.assertEqual(context["totals"]["grand_total"], Decimal("11800.00"))

    def test_non_priced_types_reject_the_items_endpoint(self):
        self.client.post(
            reverse("documents:create", args=[self.project.pk, "proposal"]),
            {"title": "Proposal", "brief": "A brief."})
        proposal = Document.objects.get(title="Proposal")
        self._save_items(proposal, [("Design", "1", "", "10000", "18")])
        self.assertEqual(proposal.line_items.count(), 0)

    def test_invoice_exports_to_pdf_with_its_figures(self):
        from io import BytesIO

        from pypdf import PdfReader

        from .pdf import render_document_pdf

        document = self._create_invoice()
        self._save_items(document, [("Landing page design", "2", "pages", "25000", "18")])
        document.refresh_from_db()
        text = "".join(page.extract_text() or ""
                       for page in PdfReader(BytesIO(render_document_pdf(document))).pages)
        # xhtml2pdf has no ₹ glyph, so pdf.py substitutes "Rs. " — either is fine
        self.assertTrue("59,000.00" in text, f"total missing from PDF: {text[:400]}")
        self.assertIn("Landing page design", text)


@override_settings(GROQ_API_KEYS=[])
class LineItemModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_doctypes", verbosity=0)
        cls.client_obj = Client.objects.create(name="Testco")
        cls.document = Document.objects.create(
            project=cls.client_obj.projects.first(),
            document_type=DocumentType.objects.get(slug="invoice"),
            title="Invoice")

    def test_row_amounts_are_derived_never_stored(self):
        item = LineItem.objects.create(
            document=self.document, description="Design",
            quantity=Decimal("3"), unit_price=Decimal("1500.50"),
            tax_rate=Decimal("18"))
        self.assertEqual(item.amount, Decimal("4501.50"))
        self.assertEqual(item.tax_amount, Decimal("810.27"))
        self.assertEqual(item.total, Decimal("5311.77"))

        # change the quantity and every figure follows — no stale column to sync
        item.quantity = Decimal("6")
        self.assertEqual(item.amount, Decimal("9003.00"))
