from django.conf import settings
from django.db import models

from core.models import TimeStampedModel
from projects.models import Project


class Payment(TimeStampedModel):
    """Money actually RECEIVED for a project.

    Deliberately decoupled from invoices: an invoice is a document we send,
    a Payment is cash in hand. The `invoice` FK is an optional back-reference
    only — rollups never go through it.
    """

    class Type(models.TextChoices):
        ADVANCE = "ADVANCE", "Advance"
        MILESTONE = "MILESTONE", "Milestone"
        FINAL = "FINAL", "Final settlement"
        OTHER = "OTHER", "Other"

    class Method(models.TextChoices):
        UPI = "UPI", "UPI"
        BANK_TRANSFER = "BANK_TRANSFER", "Bank transfer"
        CASH = "CASH", "Cash"
        CHEQUE = "CHEQUE", "Cheque"
        CARD = "CARD", "Card"
        OTHER = "OTHER", "Other"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_type = models.CharField(max_length=12, choices=Type.choices, default=Type.MILESTONE)
    method = models.CharField(max_length=16, choices=Method.choices, default=Method.UPI)
    reference = models.CharField(max_length=120, blank=True,
                                 help_text="Txn id / cheque no / UPI ref")
    received_on = models.DateField()
    notes = models.TextField(blank=True)
    invoice = models.ForeignKey(
        "documents.Document", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payments",
        help_text="Optional: the invoice this payment settles",
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["-received_on", "-created_at"]

    def __str__(self):
        return f"₹{self.amount} · {self.project.name} · {self.received_on}"

    def get_absolute_url(self):
        return self.project.get_absolute_url()
