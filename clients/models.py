from django.conf import settings
from django.db import models
from django.urls import reverse

from core.models import TimeStampedModel


class Client(TimeStampedModel):
    """A client company. Everything in the system hangs off this."""
    name = models.CharField(max_length=200)
    website = models.URLField(blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)

    # Billing details (used to auto-fill invoices/contracts)
    gstin = models.CharField("GSTIN", max_length=20, blank=True)
    billing_address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, default="India")

    notes = models.TextField(blank=True)
    is_archived = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    # --- tenancy ---
    # Which partition this record belongs to. NULL reads as the home
    # workspace, so rows written before partner access shipped stay ours.
    # See core.tenancy for how it reaches every query.
    workspace = models.ForeignKey(
        "core.Workspace", on_delete=models.CASCADE, null=True, blank=True,
        related_name="clients",
    )

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["workspace", "is_archived"])]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("clients:detail", args=[self.pk])

    @property
    def primary_contact(self):
        return self.contacts.filter(is_primary=True).first() or self.contacts.first()

    # --- finance rollups across this client's non-archived projects ---
    # Single-currency (INR) assumption for now: amounts are summed without
    # conversion, matching Project.currency's INR default.

    @property
    def total_value(self):
        from decimal import Decimal
        return (
            self.projects.filter(is_archived=False)
            .aggregate(s=models.Sum("budget"))["s"] or Decimal("0")
        )

    @property
    def total_received(self):
        from decimal import Decimal
        from finance.models import Payment
        return (
            Payment.objects.filter(project__client=self, project__is_archived=False)
            .aggregate(s=models.Sum("amount"))["s"] or Decimal("0")
        )

    @property
    def outstanding(self):
        return self.total_value - self.total_received


class Contact(TimeStampedModel):
    """A person at the client company."""
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="contacts")
    name = models.CharField(max_length=150)
    designation = models.CharField(max_length=120, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    is_primary = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_primary", "name"]

    def __str__(self):
        return f"{self.name} ({self.client.name})"
