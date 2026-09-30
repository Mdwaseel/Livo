from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base: created/updated timestamps on every record."""
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Workspace(models.Model):
    """A data partition — one agency's world inside the same install.

    The RBAC matrix answers *what may you do*; this answers *whose data is it*.
    Every root record (client, project, calendar event, activity entry) carries
    a workspace, and every list, chart and rollup is narrowed to the workspaces
    the viewer is allowed into. See `core.tenancy` for the engine.

    Exactly one row is the HOME workspace: the agency that owns the install.
    Its super admins can look into partner workspaces (and across all of them
    at once); a partner never sees out of their own.
    """

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=60, unique=True)
    # The install's own agency. Enforced to a single row by `save`, because a
    # second home workspace would silently hand a partner the whole database.
    is_home = models.BooleanField(
        default=False,
        help_text="The agency that owns this install. Exactly one workspace.")
    is_active = models.BooleanField(default=True)
    # Shown on the workspace switcher so a partner's rows are recognisable at a
    # glance when a home admin is looking across everything.
    color = models.CharField(max_length=7, default="#6366F1")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_home", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            base = slugify(self.name)[:55] or "workspace"
            slug, n = base, 2
            while Workspace.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{n}"
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)
        if self.is_home:
            Workspace.objects.filter(is_home=True).exclude(pk=self.pk).update(
                is_home=False)

    @classmethod
    def home(cls):
        """The install's own workspace, created on first use.

        Named after AgencySettings so the two read alike, and self-healing for
        the same reason: a database that somehow has no home workspace would
        otherwise make every scoped query raise.
        """
        obj = cls.objects.filter(is_home=True).order_by("pk").first()
        if obj is None:
            from .models import AgencySettings
            try:
                name = AgencySettings.objects.values_list(
                    "agency_name", flat=True).first() or "Livo Digital"
            except Exception:
                name = "Livo Digital"
            obj = cls.objects.create(name=name, slug="home", is_home=True)
        return obj

    @property
    def member_count(self):
        return self.users.filter(is_active=True).count()


class AgencySettings(models.Model):
    """Single-row branding + defaults for the whole agency."""
    agency_name = models.CharField(max_length=200, default="Livo Digital")
    tagline = models.CharField(max_length=255, blank=True)
    logo = models.ImageField(upload_to="branding/", blank=True, null=True)
    primary_color = models.CharField(max_length=7, default="#00795B")
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.TextField(blank=True)
    # The agency's own place of supply. Compared against the client's state to
    # decide CGST+SGST (same state) vs IGST (different) on a tax invoice —
    # left blank, invoices fall back to a single undivided GST line rather
    # than guessing, because guessing wrong prints a legally wrong invoice.
    state = models.CharField(max_length=100, blank=True,
                             help_text="Place of supply, e.g. Telangana. "
                                       "Used to split GST into CGST/SGST or IGST.")
    gstin = models.CharField("GSTIN", max_length=20, blank=True)
    website = models.URLField(blank=True)
    # Paid days off per calendar year, before leave starts costing salary.
    # The floor for the whole agency; any employee can be given more on their
    # HR record. See employees.leave.
    default_annual_leave_days = models.PositiveIntegerField(
        default=18,
        help_text="Paid leave days per year, before excess is deducted from "
                  "salary. Can be raised for an individual on their HR record.")

    class Meta:
        verbose_name = "Agency settings"
        verbose_name_plural = "Agency settings"

    def __str__(self):
        return self.agency_name

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class ActivityLog(TimeStampedModel):
    """Audit trail of who did what."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    # Which partition the action happened in. Stamped from the actor's active
    # workspace so a partner's feed shows their own work and nobody else's;
    # NULL is read as the home workspace (see core.tenancy.scope).
    workspace = models.ForeignKey(
        "core.Workspace", on_delete=models.CASCADE, null=True, blank=True,
        related_name="activity",
    )
    verb = models.CharField(max_length=120)          # "created", "generated", "exported"
    target = models.CharField(max_length=200, blank=True)  # human-readable target
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["workspace", "-created_at"])]

    def __str__(self):
        return f"{self.user} {self.verb} {self.target}"


def log_activity(user, verb, target="", description=""):
    """Helper used across the app to write an audit entry."""
    from .tenancy import workspace_for_new
    actor = user if getattr(user, "is_authenticated", False) else None
    return ActivityLog.objects.create(
        user=actor, workspace=workspace_for_new(actor),
        verb=verb, target=target, description=description,
    )


class Notification(TimeStampedModel):
    """A lightweight in-app notification shown under the topbar bell."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    text = models.CharField(max_length=220)
    url = models.CharField(max_length=220, blank=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"→ {self.user}: {self.text}"


def notify(users, text, url="", exclude=None):
    """Create a notification for each user (skipping `exclude`, usually the actor)."""
    recipients = {u.pk: u for u in users if u.is_active}
    if exclude is not None and getattr(exclude, "pk", None) in recipients:
        del recipients[exclude.pk]
    Notification.objects.bulk_create(
        [Notification(user=u, text=text, url=url) for u in recipients.values()]
    )
