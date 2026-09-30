from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.models import Client
from documents.models import Document, DocumentType
from projects.models import Project
from .models import Payment

User = get_user_model()


class PaymentRollupTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", password="x", role="OWNER")
        self.client_obj = Client.objects.create(name="Rollup Co")
        self.project = Project.objects.create(
            client=self.client_obj, name="Site", budget=Decimal("100000"),
        )

    def _pay(self, amount, **kw):
        return Payment.objects.create(
            project=self.project, amount=Decimal(amount),
            received_on=kw.pop("received_on", date.today()), **kw,
        )

    def test_project_rollups_and_status(self):
        self.assertEqual(self.project.total_received, 0)
        self.assertEqual(self.project.outstanding, Decimal("100000"))
        self.assertEqual(self.project.payment_status, "Unpaid")

        self._pay("40000")
        self.assertEqual(self.project.total_received, Decimal("40000"))
        self.assertEqual(self.project.outstanding, Decimal("60000"))
        self.assertEqual(self.project.payment_status, "Partially paid")

        self._pay("60000")
        self.assertEqual(self.project.payment_status, "Paid in full")
        self._pay("5000")
        self.assertEqual(self.project.payment_status, "Overpaid")
        self.assertEqual(self.project.outstanding, Decimal("-5000"))

    def test_client_rollups_ignore_archived_projects(self):
        archived = Project.objects.create(
            client=self.client_obj, name="Old", budget=Decimal("50000"),
            is_archived=True,
        )
        Payment.objects.create(project=archived, amount=Decimal("50000"),
                               received_on=date.today())
        self._pay("25000")
        self.assertEqual(self.client_obj.total_value, Decimal("100000"))
        self.assertEqual(self.client_obj.total_received, Decimal("25000"))
        self.assertEqual(self.client_obj.outstanding, Decimal("75000"))

    def test_record_payment_view_updates_rollups(self):
        self.client.login(username="owner", password="x")
        r = self.client.post(
            reverse("finance:payment_create", args=[self.project.pk]),
            {"amount": "12500.50", "received_on": "2026-07-10",
             "payment_type": "ADVANCE", "method": "UPI", "reference": "UTR123"},
        )
        self.assertRedirects(r, self.project.get_absolute_url())
        self.assertEqual(self.project.total_received, Decimal("12500.50"))

    def test_the_dashboard_no_longer_quotes_money(self):
        """These figures used to sit here and deliberately do not any more.

        The dashboard is the screen that ends up on a projector, so revenue
        and outstanding moved to the Business overview behind their own
        permission. Asserted from the finance side as well as the core side
        because this is the app that would put them back.
        """
        self._pay("30000", received_on=date.today())
        self.client.login(username="owner", password="x")
        r = self.client.get(reverse("core:dashboard"))
        self.assertNotContains(r, "Received this month")
        self.assertNotContains(r, "Total outstanding")
        self.assertNotContains(r, "₹30,000")

    def test_the_business_overview_shows_finance_stats(self):
        """Where those figures went. Same numbers, narrower audience."""
        self._pay("30000", received_on=date.today())
        self.client.login(username="owner", password="x")
        r = self.client.get(reverse("core:business"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["finance"]["revenue"], Decimal("30000.00"))
        self.assertEqual(r.context["finance"]["outstanding"], Decimal("70000.00"))
        self.assertContains(r, "Received in range")
        self.assertContains(r, "Outstanding")

    def test_accordion_shows_outstanding(self):
        self._pay("40000")
        self.client.login(username="owner", password="x")
        r = self.client.get(reverse("projects:list"))
        self.assertContains(r, "₹60,000 due")


class PaymentPermissionTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(name="Perm Co")
        self.project = Project.objects.create(client=self.client_obj, name="Site")
        self.acct = User.objects.create_user("acct", password="x", role="ACCT")
        self.dev = User.objects.create_user("dev", password="x", role="DEV")
        self.project.members.add(self.acct, self.dev)

    def test_accounts_role_can_record(self):
        self.client.login(username="acct", password="x")
        r = self.client.post(
            reverse("finance:payment_create", args=[self.project.pk]),
            {"amount": "1000", "received_on": "2026-07-01"},
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Payment.objects.count(), 1)

    def test_developer_role_is_denied(self):
        self.client.login(username="dev", password="x")
        r = self.client.post(
            reverse("finance:payment_create", args=[self.project.pk]),
            {"amount": "1000", "received_on": "2026-07-01"},
        )
        self.assertEqual(r.status_code, 302)   # redirected away, not created
        self.assertEqual(Payment.objects.count(), 0)

    def test_buttons_hidden_for_non_finance_roles(self):
        Payment.objects.create(project=self.project, amount=Decimal("1000"),
                               received_on=date.today())
        self.client.login(username="dev", password="x")
        r = self.client.get(self.project.get_absolute_url())
        self.assertNotContains(r, "Record payment")
        self.assertNotContains(r, "payment_delete")


class FinanceVisibilityTests(TestCase):
    """Finance figures and financial documents (invoice/quotation/proposal) are
    gated on finance.view — the Finance → view switch in the RBAC matrix.
    Delivery roles like Developer are limited to their tasks and non-financial
    documents; Accounts and the admins keep full finance visibility."""

    def setUp(self):
        self.client_obj = Client.objects.create(name="Vis Co")
        self.project = Project.objects.create(
            client=self.client_obj, name="Site", budget=Decimal("100000"))
        Payment.objects.create(project=self.project, amount=Decimal("40000"),
                               received_on=date.today())
        self.owner = User.objects.create_user("owner-v", password="x", role="OWNER")
        self.acct = User.objects.create_user("acct-v", password="x", role="ACCT")
        self.dev = User.objects.create_user("dev-v", password="x", role="DEV")
        self.project.members.add(self.owner, self.acct, self.dev)

        invoice_type = DocumentType.objects.create(
            name="Invoice", slug="invoice", category="CLOSING")
        contract_type = DocumentType.objects.create(
            name="Contract", slug="contract", category="CLOSING")
        self.invoice = Document.objects.create(
            project=self.project, document_type=invoice_type,
            title="INV-VIS", created_by=self.owner)
        self.contract = Document.objects.create(
            project=self.project, document_type=contract_type,
            title="CON-VIS", created_by=self.owner)

    def test_developer_cannot_see_money_or_financial_documents(self):
        self.client.login(username="dev-v", password="x")
        # dashboard money tiles hidden
        d = self.client.get(reverse("core:dashboard"))
        self.assertNotContains(d, "Received this month")
        self.assertNotContains(d, "Total outstanding")
        # accordion outstanding balance hidden
        self.assertNotContains(
            self.client.get(reverse("projects:list")), "Outstanding balance")
        # client Finances card hidden
        self.assertNotContains(
            self.client.get(self.client_obj.get_absolute_url()), "Finances")
        # invoice hidden from the library, the contract still shown
        lib = self.client.get(reverse("documents:list"))
        self.assertNotContains(lib, "INV-VIS")
        self.assertContains(lib, "CON-VIS")
        # invoice detail blocked, contract detail fine
        self.assertEqual(
            self.client.get(self.invoice.get_absolute_url()).status_code, 302)
        self.assertEqual(
            self.client.get(self.contract.get_absolute_url()).status_code, 200)

    def test_accounts_keeps_finance_visibility(self):
        """`finance.view` still means invoices — it just no longer means the
        agency's cash position."""
        self.client.login(username="acct-v", password="x")
        d = self.client.get(reverse("core:dashboard"))
        self.assertContains(d, "INV-VIS")   # invoice in recent documents
        self.assertEqual(
            self.client.get(self.invoice.get_absolute_url()).status_code, 200)

    def test_accounts_does_not_inherit_the_agency_cash_position(self):
        """The reason the Business overview got its own module key.

        Accounts holds `finance.view` because somebody has to work the
        invoices. Reusing that key for the money screen would have moved the
        page without narrowing who reads it, which is the entire point of
        having moved it.
        """
        self.client.login(username="acct-v", password="x")
        self.assertNotContains(
            self.client.get(reverse("core:dashboard")), reverse("core:business"))
        self.assertEqual(
            self.client.get(reverse("core:business")).status_code, 302)
