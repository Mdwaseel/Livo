from decimal import Decimal

from django.conf import settings
from django.db import models
from django.urls import reverse

from core.models import TimeStampedModel
from projects.models import Project


class DocumentType(TimeStampedModel):
    """
    The engine's configuration. Each document (Proposal, Contract, Invoice...)
    is a ROW here, not a separate app. To add a new document type you add a
    DocumentType with its own form fields, AI prompt, and template - no new code.
    """

    class Category(models.TextChoices):
        PRE_DEAL = "PRE_DEAL", "Pre-Deal"          # proposal, quotation
        CLOSING = "CLOSING", "Closing"             # contract, onboarding, advance invoice
        DELIVERY = "DELIVERY", "Delivery"          # srs, mom, reports, qa
        CLOSEOUT = "CLOSEOUT", "Close-out"         # handover, maintenance, final invoice

    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    category = models.CharField(max_length=12, choices=Category.choices,
                                default=Category.PRE_DEAL)
    description = models.CharField(max_length=255, blank=True)
    icon = models.CharField(max_length=40, blank=True, help_text="icon name/emoji")

    # JSON list of form fields the user fills in, e.g.
    # [{"name": "scope", "label": "Project scope", "type": "textarea", "required": true}]
    form_schema = models.JSONField(default=list, blank=True)

    # The instruction sent to the AI. Supports {placeholders} filled from the
    # form answers + client/project context.
    ai_prompt = models.TextField(blank=True)

    # Priced types (invoice, quotation) carry LineItems and render their body
    # from those rows instead of from AI prose. A flag rather than a hardcoded
    # slug list, so a new priced type is still a row, not a code change.
    is_priced = models.BooleanField(
        default=False,
        help_text="Itemised pricing: line items, GST and totals computed server-side.")

    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category", "order", "name"]

    def __str__(self):
        return self.name


class Template(TimeStampedModel):
    """A visual document template designed with images, not HTML/CSS.

    Three A4 images plus one drawn rectangle: `cover_image` becomes page 1 of
    the exported PDF, `content_background` repeats behind every content page
    with the AI-generated content_html flowing into the drawn region, and
    `back_image` is the constant closing page of every document. The region is
    stored as PERCENTAGES (0-100) of the A4 page so it is resolution-independent
    at export time.

    All three images are independently optional and pdf.py skips whichever is
    absent, so a content-background-only template — one constant content page
    behind every document, no cover and no back — is a first-class design.
    """
    name = models.CharField(max_length=120)
    document_type = models.ForeignKey(DocumentType, on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name="templates")
    cover_image = models.ImageField(upload_to="templates/covers/", blank=True, null=True,
                                    help_text="Full-page cover design (page 1, A4)")
    content_background = models.ImageField(upload_to="templates/content/", blank=True,
                                           null=True,
                                           help_text="Background repeated on content pages (A4)")
    back_image = models.ImageField(upload_to="templates/backs/", blank=True, null=True,
                                   help_text="Constant last page (back / thank-you, A4)")
    # Drawn content region as % of the A4 page.
    content_x = models.FloatField(default=8.0)
    content_y = models.FloatField(default=12.0)
    content_width = models.FloatField(default=84.0)
    content_height = models.FloatField(default=76.0)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def region_label(self):
        return (f"{self.content_width:.0f} × {self.content_height:.0f}% "
                f"at {self.content_x:.0f}, {self.content_y:.0f}")

    @property
    def has_visual_pages(self):
        """True when the template can drive the image-based PDF renderer."""
        return bool(self.cover_image or self.content_background or self.back_image)


class Document(TimeStampedModel):
    """A single generated document instance, always tied to a project (and thus a client)."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        REVIEW = "REVIEW", "In Review"
        APPROVED = "APPROVED", "Approved"
        SENT = "SENT", "Sent"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="documents")
    document_type = models.ForeignKey(DocumentType, on_delete=models.PROTECT)
    title = models.CharField(max_length=200)
    number = models.CharField(max_length=60, blank=True,
                              help_text="Auto-number for invoices/quotations")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    # When this has to be reviewed by. Set when the document is sent for review
    # and cleared once it's signed off. It lives here rather than in the
    # calendar app that surfaces it: a review deadline is a fact about the
    # document, and the document page is where it's set.
    review_due_date = models.DateField(
        null=True, blank=True,
        help_text="Shown on the calendar as a review deadline.")

    # Answers to the DocumentType's form_schema, kept for regeneration.
    form_data = models.JSONField(default=dict, blank=True)

    current_version = models.ForeignKey(
        "DocumentVersion", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    template = models.ForeignKey(Template, on_delete=models.SET_NULL, null=True, blank=True)

    is_archived = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Auto-numbers must be unique — services.assign_number computes the
            # next sequence by scanning existing ones, which two concurrent
            # requests can do simultaneously. The DB is the only thing that can
            # actually settle that race; assign_number retries when it loses.
            # Unnumbered types keep number="" and are excluded.
            models.UniqueConstraint(
                fields=["number"], condition=~models.Q(number=""),
                name="unique_document_number",
            ),
        ]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("documents:detail", args=[self.pk])

    @property
    def client(self):
        return self.project.client


class LineItem(TimeStampedModel):
    """One priced row on an invoice or quotation.

    Money never comes out of the AI. The model proposes rows from the brief,
    a human edits them here, and every sum — line amount, GST per rate,
    grand total — is done in Decimal by documents.pricing. The document's
    rendered body is regenerated from these rows, so what the PDF shows and
    what the totals say cannot drift apart.
    """
    document = models.ForeignKey(Document, on_delete=models.CASCADE,
                                 related_name="line_items")
    description = models.CharField(max_length=300)
    quantity = models.DecimalField(max_digits=12, decimal_places=2,
                                   default=Decimal("1.00"))
    unit = models.CharField(max_length=20, blank=True,
                            help_text='Optional unit, e.g. "hrs", "pages", "mo"')
    unit_price = models.DecimalField(max_digits=12, decimal_places=2,
                                     default=Decimal("0.00"))
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2,
                                   default=Decimal("18.00"),
                                   help_text="GST % charged on this line")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "pk"]

    def __str__(self):
        return f"{self.description} × {self.quantity}"

    # The three per-row figures. Kept as properties rather than stored columns
    # so an edited quantity can never leave a stale amount behind.

    @property
    def amount(self):
        """Line value before tax."""
        from .pricing import money
        return money(self.quantity * self.unit_price)

    @property
    def tax_amount(self):
        from .pricing import money
        return money(self.amount * self.tax_rate / Decimal("100"))

    @property
    def total(self):
        return self.amount + self.tax_amount


class DocumentVersion(TimeStampedModel):
    """Immutable snapshot of a document's rich-text content. Powers version history."""
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="versions")
    version_number = models.PositiveIntegerField(default=1)
    content_html = models.TextField(blank=True)   # what the rich-text editor produces
    note = models.CharField(max_length=200, blank=True)  # "AI draft", "manual edit"
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["-version_number"]
        unique_together = ("document", "version_number")

    def __str__(self):
        return f"{self.document.title} v{self.version_number}"


class GeneratedFile(TimeStampedModel):
    """An exported artifact (PDF/DOCX) for a specific version."""
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="files")
    version = models.ForeignKey(DocumentVersion, on_delete=models.CASCADE)
    file = models.FileField(upload_to="generated/")
    file_type = models.CharField(max_length=10, default="pdf")

    def __str__(self):
        return f"{self.document.title} ({self.file_type})"
