"""Regressions for the document-engine bugs found in the July 2026 audit."""
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Role, RolePermission
from clients.models import Client

from .access import can_view_doc_slug, can_view_finance
from .models import Document, DocumentType
from .services import assign_number

User = get_user_model()


@override_settings(GROQ_API_KEYS=[])
class SignedOffDocumentsReopenOnEditTests(TestCase):
    """Editing an APPROVED or SENT document left its status untouched, so the
    sign-off silently came to cover content nobody had approved. Work logs
    already worked the other way (projects.views.worklog_edit)."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_doctypes", verbosity=0)
        cls.owner = User.objects.create_user("owner", password="x", role="OWNER")
        cls.client_obj = Client.objects.create(name="Reopenco")
        cls.project = cls.client_obj.projects.first()

    def setUp(self):
        self.client.force_login(self.owner)
        self.client.post(
            reverse("documents:create", args=[self.project.pk, "contract"]),
            {"title": "Deal", "brief": "Terms."})
        self.doc = Document.objects.get(title="Deal")

    def _set_status(self, status):
        Document.objects.filter(pk=self.doc.pk).update(status=status)
        self.doc.refresh_from_db()

    def test_manual_edit_of_an_approved_document_returns_it_to_draft(self):
        self._set_status(Document.Status.APPROVED)

        self.client.post(reverse("documents:save", args=[self.doc.pk]),
                         {"content_html": "<p>Quietly different terms.</p>"})

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, Document.Status.DRAFT)

    def test_editing_a_sent_document_returns_it_to_draft(self):
        self._set_status(Document.Status.SENT)

        self.client.post(reverse("documents:save", args=[self.doc.pk]),
                         {"content_html": "<p>Changed after sending.</p>"})

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, Document.Status.DRAFT)

    def test_autosave_reopens_too_even_when_it_reuses_a_version(self):
        self.client.post(reverse("documents:autosave", args=[self.doc.pk]),
                         {"content_html": "<p>draft one</p>"})
        self._set_status(Document.Status.APPROVED)

        r = self.client.post(reverse("documents:autosave", args=[self.doc.pk]),
                             {"content_html": "<p>draft two</p>"})

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, Document.Status.DRAFT)
        self.assertEqual(r.json()["reopened_from"], "Approved")

    def test_regenerating_an_approved_document_reopens_it(self):
        self._set_status(Document.Status.APPROVED)

        self.client.post(reverse("documents:regenerate", args=[self.doc.pk]))

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, Document.Status.DRAFT)

    def test_a_draft_edit_leaves_the_status_alone(self):
        self.client.post(reverse("documents:save", args=[self.doc.pk]),
                         {"content_html": "<p>still a draft</p>"})

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, Document.Status.DRAFT)


class DocumentNumberUniquenessTests(TestCase):
    """assign_number used SELECT ... FOR UPDATE, which can only lock rows that
    already exist — it never stopped two creations picking the same sequence.
    The DB constraint decides now and the loser retries."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_doctypes", verbosity=0)
        cls.client_obj = Client.objects.create(name="Numberco")
        cls.project = cls.client_obj.projects.first()
        cls.invoice_type = DocumentType.objects.get(slug="invoice")

    def _doc(self, title):
        return Document.objects.create(project=self.project, title=title,
                                       document_type=self.invoice_type)

    def test_numbers_are_sequential(self):
        a, b = self._doc("A"), self._doc("B")

        self.assertEqual(assign_number(a)[-3:], "001")
        self.assertEqual(assign_number(b)[-3:], "002")

    def test_a_taken_number_is_skipped_rather_than_duplicated(self):
        first = self._doc("A")
        number = assign_number(first)
        contender = self._doc("B")

        # Simulate losing the race: the number this one would have computed is
        # already gone by the time it writes.
        self.assertNotEqual(assign_number(contender), number)
        self.assertEqual(Document.objects.filter(number=number).count(), 1)

    def test_unnumbered_types_share_the_empty_string(self):
        contract = DocumentType.objects.get(slug="contract")
        for title in ("C1", "C2"):
            Document.objects.create(project=self.project, title=title,
                                    document_type=contract)

        # The unique constraint is conditional, so blanks don't collide.
        self.assertEqual(Document.objects.filter(number="").count(), 2)


class FinancialDocVisibilityTests(TestCase):
    """Financial documents were gated on finance.view alone, so Sales — whose
    matrix grants full quotation rights — could not see a quotation."""

    def _user_with(self, role_name):
        person = User.objects.create_user(f"u{role_name}".replace(" ", ""),
                                          password="x")
        person.primary_role = Role.objects.get(name=role_name)
        person.save(update_fields=["primary_role"])
        return person

    def test_sales_sees_quotations_without_blanket_finance_access(self):
        sales = self._user_with("Sales")

        self.assertTrue(can_view_doc_slug(sales, "quotation"))
        self.assertTrue(can_view_doc_slug(sales, "proposal"))
        # Money figures stay behind finance.view — a separate switch.
        self.assertFalse(can_view_finance(sales))

    def test_accounts_still_sees_everything_through_finance_view(self):
        accounts = self._user_with("Accounts")

        self.assertTrue(can_view_finance(accounts))
        self.assertTrue(can_view_doc_slug(accounts, "invoice"))

    def test_delivery_roles_still_see_no_financial_documents(self):
        dev = self._user_with("Developer")

        self.assertFalse(can_view_doc_slug(dev, "invoice"))
        self.assertFalse(can_view_doc_slug(dev, "quotation"))
        self.assertFalse(can_view_doc_slug(dev, "proposal"))

    def test_non_financial_types_are_open_to_the_documents_module(self):
        dev = self._user_with("Developer")

        self.assertTrue(can_view_doc_slug(dev, "contract"))

    def test_revoking_the_module_hides_it_again(self):
        sales = self._user_with("Sales")
        RolePermission.objects.filter(
            role=sales.primary_role, module__key="quotations").delete()

        self.assertFalse(can_view_doc_slug(sales, "quotation"))
