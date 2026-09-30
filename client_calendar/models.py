"""What we plan and deliver for a client, and the link that lets them watch.

Why a table of its own rather than a view over tasks. A task is internal work:
it has an assignee, estimated hours, a reviewer, and a title written for the
team ("fix the broken header on mobile"). None of that belongs in front of a
client, and redacting it field by field is how something leaks the day a new
field is added. An activity is the opposite — written *for* the client from the
first keystroke: "Diwali offer reel", Instagram, Thursday 6:30pm, published,
here is the link. The two can describe the same piece of work and still be two
different records, because they are answering two different audiences.

`calendar_hub` reads these through a source adapter like any other app, so the
team sees the content schedule on the agency calendar without a second copy.
"""
import os
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.db import models
from django.db.models import F
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from core.models import TimeStampedModel


def _preview_path(instance, filename):
    """A random name, never the uploaded one.

    The original filename is often the campaign's internal working title and is
    always guessable; the public calendar serves previews through a token-checked
    view, but in development `/media/` is served directly, and a path nobody can
    guess is the cheap second lock.
    """
    extension = os.path.splitext(filename)[1].lower()[:8]
    return f"client_activity/{uuid.uuid4().hex}{extension}"


class ClientActivity(TimeStampedModel):
    """One thing on a client's calendar: planned, happening, or done."""

    class Kind(models.TextChoices):
        POST = "POST", "Feed post"
        CAROUSEL = "CAROUSEL", "Carousel"
        REEL = "REEL", "Reel / video"
        STORY = "STORY", "Story"
        AD_LAUNCH = "AD_LAUNCH", "Ad campaign launch"
        AD_CHANGE = "AD_CHANGE", "Ad optimisation"
        AD_REVIEW = "AD_REVIEW", "Campaign review"
        CREATIVE = "CREATIVE", "Creative for approval"
        REPORT = "REPORT", "Performance report"
        MEETING = "MEETING", "Meeting / call"
        WEBSITE = "WEBSITE", "Website update"
        OTHER = "OTHER", "Other"

    class Platform(models.TextChoices):
        INSTAGRAM = "INSTAGRAM", "Instagram"
        FACEBOOK = "FACEBOOK", "Facebook"
        LINKEDIN = "LINKEDIN", "LinkedIn"
        YOUTUBE = "YOUTUBE", "YouTube"
        X = "X", "X (Twitter)"
        GOOGLE_ADS = "GOOGLE_ADS", "Google Ads"
        META_ADS = "META_ADS", "Meta Ads"
        WEBSITE = "WEBSITE", "Website"
        EMAIL = "EMAIL", "Email"
        WHATSAPP = "WHATSAPP", "WhatsApp"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        PLANNED = "PLANNED", "Planned"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        APPROVAL = "APPROVAL", "Awaiting client approval"
        APPROVED = "APPROVED", "Approved by client"
        DONE = "DONE", "Done"
        POSTPONED = "POSTPONED", "Postponed"

    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE,
        related_name="client_activities")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.POST)
    platform = models.CharField(max_length=12, choices=Platform.choices, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.PLANNED)

    date = models.DateField()
    time = models.TimeField(null=True, blank=True,
                            help_text="Leave blank if it isn't tied to a time of day.")

    title = models.CharField(
        max_length=160, help_text="Written for the client — they will read this.")
    details = models.TextField(
        blank=True, help_text="Caption, what's changing, or what the review covers.")
    link = models.URLField(
        max_length=500, blank=True,
        help_text="The live post, ad preview, report or Drive folder.")
    preview = models.ImageField(upload_to=_preview_path, blank=True)

    show_to_client = models.BooleanField(
        default=True,
        help_text="Untick to keep a draft off the client's calendar.")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")

    class Meta:
        ordering = ["date", "time", "id"]
        verbose_name_plural = "client activities"
        indexes = [
            models.Index(fields=["project", "date"]),
            models.Index(fields=["date", "show_to_client"]),
        ]

    def __str__(self):
        return f"{self.title} · {self.date}"

    def get_absolute_url(self):
        return reverse("client_calendar:activity_edit", args=[self.pk])

    # --- change tracking, for the client's update emails (see updates.py) ---
    #
    # The values as they were read from the database, kept on the instance so
    # a save can tell "moved to Friday" from "fixed a typo" without querying
    # the row a second time. Every save path — the planner form, a drag on the
    # agency calendar, an approval decision — goes through `save()`, so every
    # one of them is seen.
    TRACKED_FIELDS = ("date", "time", "status", "show_to_client")

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance.remember_tracked()
        return instance

    def remember_tracked(self):
        self._tracked = {name: self.__dict__[name] for name in self.TRACKED_FIELDS
                         if name in self.__dict__}

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        self.remember_tracked()

    # --- presentation, shared by the planner and the public page ---

    @property
    def client(self):
        return self.project.client

    @property
    def family(self):
        return FAMILY_OF.get(self.kind, "other")

    @property
    def family_label(self):
        return FAMILIES[self.family][0]

    @property
    def kind_icon(self):
        return self.kind.lower()

    @property
    def platform_icon(self):
        return self.platform.lower() if self.platform else ""

    @property
    def short_kind(self):
        return SHORT_KIND.get(self.kind, self.get_kind_display())

    @property
    def is_done(self):
        return self.status == self.Status.DONE

    @property
    def needs_approval(self):
        return self.status == self.Status.APPROVAL

    @property
    def client_status_label(self):
        """What the status means to the person reading the shared calendar.

        "Done" is right for the team and flat for a client: a post is
        *published*, a campaign is *live*, a review is *completed*. And the
        approval state is addressed to them, so it says so.
        """
        if self.status == self.Status.DONE:
            return DONE_LABEL.get(self.kind, "Completed")
        if self.status == self.Status.APPROVAL:
            return "Needs your approval"
        if self.status == self.Status.APPROVED:
            return "Approved"
        return self.get_status_display()

    @property
    def latest_approval(self):
        """The newest approval request, when the view prefetched them.

        Only ever read from `approval_list` (see `views.planner`), never
        queried here: the card is rendered once per activity, and a lookup per
        card is how a month of forty posts becomes forty-one queries.
        """
        requests = getattr(self, "approval_list", None)
        return requests[0] if requests else None

    @property
    def status_icon(self):
        return {
            self.Status.DONE: "check",
            self.Status.APPROVED: "badge_check",
            self.Status.APPROVAL: "alert",
            self.Status.IN_PROGRESS: "progress",
            self.Status.POSTPONED: "pause",
        }.get(self.status, "clock")


# Colour and filter groups. Twelve kinds in twelve colours is a legend nobody
# reads; four families a client already thinks in is one they don't need to.
FAMILIES = {
    "content": ("Social content", ("POST", "CAROUSEL", "REEL", "STORY")),
    "ads": ("Ad campaigns", ("AD_LAUNCH", "AD_CHANGE", "AD_REVIEW")),
    "creative": ("Creative & approvals", ("CREATIVE",)),
    "reporting": ("Reports & meetings", ("REPORT", "MEETING")),
    "other": ("Website & other", ("WEBSITE", "OTHER")),
}
FAMILY_OF = {kind: key for key, (_, kinds) in FAMILIES.items() for kind in kinds}

SHORT_KIND = {
    "POST": "Post", "CAROUSEL": "Carousel", "REEL": "Reel", "STORY": "Story",
    "AD_LAUNCH": "Ad launch", "AD_CHANGE": "Ad update", "AD_REVIEW": "Ad review",
    "CREATIVE": "Creative", "REPORT": "Report", "MEETING": "Meeting",
    "WEBSITE": "Website", "OTHER": "Update",
}

DONE_LABEL = {
    "POST": "Published", "CAROUSEL": "Published", "REEL": "Published",
    "STORY": "Published", "AD_LAUNCH": "Live", "AD_CHANGE": "Applied",
    "CREATIVE": "Delivered", "REPORT": "Shared", "WEBSITE": "Live",
}


class ClientCalendarLink(TimeStampedModel):
    """The secret URL a client opens. One per client.

    The token is the whole of the authentication, so it is treated like a
    password with a public face: 32 URL-safe characters from `secrets` (about
    190 bits — not guessable, not enumerable), compared by an indexed equality
    lookup, and replaced outright rather than edited. Rotating it is how a link
    forwarded to the wrong person is taken back: the old URL stops working the
    moment the new one exists.

    One per client rather than one per project because the person receiving it
    usually owns the relationship, not a single workstream. The page filters by
    project for clients who have several.
    """

    client = models.OneToOneField(
        "clients.Client", on_delete=models.CASCADE, related_name="calendar_link")
    token = models.CharField(max_length=64, unique=True, editable=False)
    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    rotated_at = models.DateTimeField(null=True, blank=True)

    # Opens by somebody who is not signed in to the app — i.e. the client, not a
    # teammate checking what the client will see.
    view_count = models.PositiveIntegerField(default=0)
    last_viewed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Calendar link · {self.client}"

    @staticmethod
    def new_token():
        return secrets.token_urlsafe(24)

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = self.new_token()
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("client_calendar_public:calendar", args=[self.token])

    def rotate(self):
        self.token = self.new_token()
        self.rotated_at = timezone.now()
        self.is_active = True
        self.view_count = 0
        self.last_viewed_at = None
        self.save()

    def record_view(self):
        """Counted in SQL, not read-modify-write, so two tabs opening at once
        both count."""
        now = timezone.now()
        type(self).objects.filter(pk=self.pk).update(
            view_count=F("view_count") + 1, last_viewed_at=now)


# ===========================================================================
# Client approvals
# ===========================================================================
#
# The calendar link is shared with a company; an approval is asked of a person.
# That difference decides the whole design. Anyone the client forwards the
# calendar to may look at it, but "approved" has to mean *this* person said yes
# — so an approval is reached through a link emailed to one reviewer, and the
# first time that link is opened in a browser the reviewer proves they own the
# inbox with a six-digit code sent to it. A forwarded email gets the next reader
# as far as "we've sent a code to pr•••@acme.com", and no further.


def _approval_path(instance, filename):
    """Random, like previews: the original name is an internal working title."""
    extension = os.path.splitext(filename)[1].lower()[:8]
    return f"client_approvals/{uuid.uuid4().hex}{extension}"


class ClientReviewer(TimeStampedModel):
    """Someone at a client who can approve work. One row per client + email.

    Kept separate from `clients.Contact` because the two drift: a contact may
    have no email, and the person who signs off creative is often somebody the
    account manager typed in once ("send it to our brand lead too") and never
    added to the CRM. `contact` links them when they are the same person.

    The token identifies the reviewer across every request they are sent, so
    one verified browser stays verified for all of them rather than asking for
    a fresh code on each email.
    """

    client = models.ForeignKey(
        "clients.Client", on_delete=models.CASCADE, related_name="reviewers")
    contact = models.ForeignKey(
        "clients.Contact", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    name = models.CharField(max_length=150)
    email = models.EmailField()
    token = models.CharField(max_length=64, unique=True, editable=False)
    is_active = models.BooleanField(default=True)

    # Bumped to sign every remembered browser out at once. The device cookie
    # carries the value it was issued under, so a stale one simply stops
    # matching — nothing has to find and delete it.
    device_epoch = models.PositiveIntegerField(default=1)

    # The emailed code. Only a keyed hash, as with LoginOTP: a read of this
    # table must not hand anybody a working code.
    code_hash = models.CharField(max_length=64, blank=True)
    code_sent_at = models.DateTimeField(null=True, blank=True)
    code_expires_at = models.DateTimeField(null=True, blank=True)
    code_attempts = models.PositiveSmallIntegerField(default=0)

    verified_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    # --- calendar update emails (see updates.py) ---
    # Separate from approval access on purpose: somebody can want to hear what
    # changed on the calendar without being the person who signs things off,
    # and turning these off must never stop an approval request reaching them.
    class Updates(models.TextChoices):
        SOON = "SOON", "As things change"
        DAILY = "DAILY", "Daily summary"
        WEEKLY = "WEEKLY", "Weekly summary"
        OFF = "OFF", "Off"

    update_frequency = models.CharField(max_length=8, choices=Updates.choices,
                                        default=Updates.OFF)
    # Changes recorded after this moment haven't been emailed to this person
    # yet. Set to "now" when they subscribe, so nobody's first email is a
    # backlog of everything that ever happened.
    updates_since = models.DateTimeField(null=True, blank=True)
    last_update_email_at = models.DateTimeField(null=True, blank=True)
    # Set when the person turns update emails off themselves — the preferences
    # page, or their mail app's unsubscribe button. The team can't switch them
    # back on, and a one-off "it's live" email from the activity form skips
    # them too. Only they can undo it.
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["client", "email"],
                                    name="client_reviewer_unique_email"),
        ]

    def __str__(self):
        return f"{self.name} <{self.email}> · {self.client}"

    def save(self, *args, **kwargs):
        self.email = (self.email or "").strip().lower()
        if not self.token:
            self.token = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    @property
    def first_name(self):
        return (self.name or "").split(" ")[0] or self.email

    @property
    def masked_email(self):
        """pr•••@acme.com — enough to recognise, not enough to harvest."""
        local, _, domain = self.email.partition("@")
        return f"{local[:2]}{'•' * 3}@{domain}" if domain else self.email

    def get_absolute_url(self):
        return reverse("client_review:inbox", args=[self.token])

    def review_url(self, approval):
        return reverse("client_review:detail", args=[self.token, approval.pk])

    # --- the emailed code ---

    CODE_SALT = "livo.client-review-code"

    def _hash(self, code):
        return salted_hmac(self.CODE_SALT, f"{self.pk}:{code}",
                           algorithm="sha256").hexdigest()

    def issue_code(self):
        """Mint a code, replacing any outstanding one. Returns the plaintext,
        which exists only long enough to be put in an email."""
        code = "".join(str(secrets.randbelow(10)) for _ in range(6))
        now = timezone.now()
        self.code_hash = self._hash(code)
        self.code_sent_at = now
        self.code_expires_at = now + timedelta(
            seconds=getattr(settings, "CLIENT_REVIEW_CODE_TTL_SECONDS", 15 * 60))
        self.code_attempts = 0
        self.save(update_fields=["code_hash", "code_sent_at", "code_expires_at",
                                 "code_attempts", "updated_at"])
        return code

    def seconds_until_resend(self):
        if not self.code_sent_at:
            return 0
        cooldown = getattr(settings, "CLIENT_REVIEW_CODE_COOLDOWN_SECONDS", 45)
        elapsed = (timezone.now() - self.code_sent_at).total_seconds()
        return max(0, int(cooldown - elapsed))

    def check_code(self, code):
        """"ok", "wrong", "expired" or "locked". Consumes the code on success."""
        code = "".join(ch for ch in (code or "") if ch.isdigit())
        if not self.code_hash or not self.code_expires_at \
                or timezone.now() >= self.code_expires_at:
            return "expired"
        if self.code_attempts >= getattr(settings, "CLIENT_REVIEW_CODE_MAX_ATTEMPTS", 5):
            return "locked"
        if not constant_time_compare(self.code_hash, self._hash(code)):
            # In SQL, so two guesses racing each other both count.
            type(self).objects.filter(pk=self.pk).update(
                code_attempts=F("code_attempts") + 1)
            self.code_attempts += 1
            return "wrong"
        self.code_hash = ""
        self.code_expires_at = None
        self.code_attempts = 0
        self.verified_at = timezone.now()
        self.save(update_fields=["code_hash", "code_expires_at", "code_attempts",
                                 "verified_at", "updated_at"])
        return "ok"

    def sign_out_devices(self):
        self.device_epoch += 1
        self.save(update_fields=["device_epoch", "updated_at"])


class ApprovalRequest(TimeStampedModel):
    """One version of one piece of work, put in front of the client to sign off.

    A new version is a new row, not an edit. "Approved" has to point at exactly
    what was approved — the caption as it read and the files as they were — and
    a record that can be edited after the fact can't do that. So a request that
    comes back with changes stays as it was, with the client's feedback on it,
    and version 2 is sent alongside it.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Waiting for the client"
        APPROVED = "APPROVED", "Approved"
        CHANGES = "CHANGES", "Changes requested"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"

    activity = models.ForeignKey(
        ClientActivity, on_delete=models.CASCADE, related_name="approval_requests")
    version = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.PENDING)

    message = models.TextField(
        blank=True, help_text="A note to the client: what to look at, what you need.")
    content = models.TextField(
        blank=True, help_text="The copy to approve — caption, blog text, ad text.")
    respond_by = models.DateField(null=True, blank=True)

    reviewers = models.ManyToManyField(
        ClientReviewer, through="ApprovalRecipient", related_name="approval_requests")

    decided_by = models.ForeignKey(
        ClientReviewer, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    feedback = models.TextField(blank=True)

    # What the activity's status was before it went out for approval, so
    # withdrawing the request puts the calendar back the way it was.
    status_before = models.CharField(max_length=12, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["status", "activity"])]

    def __str__(self):
        return f"{self.activity.title} · v{self.version} · {self.get_status_display()}"

    def get_absolute_url(self):
        return reverse("client_calendar:approval_detail", args=[self.pk])

    @property
    def client(self):
        return self.activity.project.client

    @property
    def is_pending(self):
        return self.status == self.Status.PENDING

    @property
    def is_overdue(self):
        return (self.is_pending and self.respond_by is not None
                and self.respond_by < timezone.localdate())

    @property
    def tone(self):
        return {
            self.Status.PENDING: "warn",
            self.Status.APPROVED: "ok",
            self.Status.CHANGES: "danger",
        }.get(self.status, "gray")

    @property
    def client_status_label(self):
        return {
            self.Status.PENDING: "Waiting for your review",
            self.Status.APPROVED: "Approved",
            self.Status.CHANGES: "Changes requested",
            self.Status.WITHDRAWN: "Replaced or withdrawn",
        }[self.status]


class ApprovalRecipient(models.Model):
    """Who a request was sent to, and whether they have looked at it yet."""

    request = models.ForeignKey(
        ApprovalRequest, on_delete=models.CASCADE, related_name="recipients")
    reviewer = models.ForeignKey(
        ClientReviewer, on_delete=models.CASCADE, related_name="deliveries")
    email_count = models.PositiveSmallIntegerField(default=0)
    last_emailed_at = models.DateTimeField(null=True, blank=True)
    first_opened_at = models.DateTimeField(null=True, blank=True)
    last_opened_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["reviewer__name"]
        constraints = [
            models.UniqueConstraint(fields=["request", "reviewer"],
                                    name="approval_recipient_unique"),
        ]

    def __str__(self):
        return f"{self.reviewer} ← {self.request}"


class ApprovalAsset(models.Model):
    """A file uploaded for review, or a link to one (Drive, Figma, a staging URL)."""

    IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "gif", "webp")
    VIDEO_EXTENSIONS = ("mp4", "webm", "mov")
    AUDIO_EXTENSIONS = ("mp3", "wav")

    request = models.ForeignKey(
        ApprovalRequest, on_delete=models.CASCADE, related_name="assets")
    file = models.FileField(upload_to=_approval_path, blank=True, max_length=200)
    url = models.URLField(max_length=500, blank=True)
    name = models.CharField(max_length=200, blank=True)
    size = models.PositiveIntegerField(default=0)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.display_name

    @property
    def is_link(self):
        return not self.file and bool(self.url)

    @property
    def extension(self):
        source = self.file.name if self.file else ""
        return os.path.splitext(source)[1].lower().lstrip(".")

    @property
    def is_image(self):
        return bool(self.file) and self.extension in self.IMAGE_EXTENSIONS

    @property
    def is_video(self):
        return bool(self.file) and self.extension in self.VIDEO_EXTENSIONS

    @property
    def is_audio(self):
        return bool(self.file) and self.extension in self.AUDIO_EXTENSIONS

    @property
    def is_pdf(self):
        return bool(self.file) and self.extension == "pdf"

    @property
    def host(self):
        netloc = urlparse(self.url).netloc.lower() if self.url else ""
        return netloc[4:] if netloc.startswith("www.") else netloc

    @property
    def display_name(self):
        if self.name:
            return self.name
        if self.is_link:
            return self.host or self.url
        return os.path.basename(self.file.name) if self.file else "Attachment"

    @property
    def icon(self):
        if self.is_link:
            return "link"
        if self.is_image:
            return "image"
        if self.is_video:
            return "reel"
        if self.is_pdf or self.extension in ("doc", "docx", "txt", "rtf", "odt"):
            return "report"
        return "file"

    @property
    def size_label(self):
        from core.uploads import human_size
        return human_size(self.size) if self.size else ""


class ApprovalEvent(models.Model):
    """The request's history, as both sides see it: sent, opened, decided."""

    class Kind(models.TextChoices):
        SENT = "SENT", "Sent for approval"
        REMINDED = "REMINDED", "Reminder sent"
        OPENED = "OPENED", "Opened"
        APPROVED = "APPROVED", "Approved"
        CHANGES = "CHANGES", "Requested changes"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"

    request = models.ForeignKey(
        ApprovalRequest, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    reviewer = models.ForeignKey(
        ClientReviewer, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    note = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.get_kind_display()} · {self.request_id}"

    @property
    def who(self):
        if self.reviewer_id:
            return self.reviewer.name
        if self.actor_id:
            return self.actor.get_full_name() or self.actor.username
        return ""


# ===========================================================================
# What changed on the calendar, for the client's update emails
# ===========================================================================

class ClientUpdate(models.Model):
    """One change a client would care about, recorded as it happens.

    A log rather than a diff taken at send time, because the email has to be
    able to say "moved from Tuesday to Thursday" and "taken off the calendar" —
    things the current row can no longer tell you. The activity's title, kind,
    platform and dates are copied in, so a removed post can still be named.

    Only client-visible activities are ever logged; see `updates.record_saved`.
    """

    class Kind(models.TextChoices):
        ADDED = "ADDED", "Newly planned"
        MOVED = "MOVED", "Rescheduled"
        DONE = "DONE", "Published or done"
        POSTPONED = "POSTPONED", "Postponed"
        RESUMED = "RESUMED", "Back on the plan"
        REMOVED = "REMOVED", "Taken off the calendar"

    client = models.ForeignKey(
        "clients.Client", on_delete=models.CASCADE, related_name="calendar_updates")
    activity = models.ForeignKey(
        ClientActivity, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    # The activity's id as a plain number. The foreign key is nulled when the
    # activity is deleted, and without this a post added and then deleted
    # before the email went could no longer be recognised as the same post.
    activity_ref = models.PositiveIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices)

    title = models.CharField(max_length=160)
    activity_kind = models.CharField(max_length=12, blank=True)
    platform = models.CharField(max_length=12, blank=True)
    date = models.DateField()
    time = models.TimeField(null=True, blank=True)
    old_date = models.DateField(null=True, blank=True)
    old_time = models.TimeField(null=True, blank=True)

    # People already told about this change directly ("Email the client about
    # this now" on the activity form), so their next summary doesn't repeat it.
    announced_to = models.ManyToManyField(ClientReviewer, blank=True, related_name="+")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["client", "created_at"])]

    def __str__(self):
        return f"{self.get_kind_display()} · {self.title}"


@receiver(post_save, sender=ClientActivity)
def _log_client_visible_change(sender, instance, created, raw=False, **kwargs):
    # Deletes are recorded by the delete view instead of a post_delete signal:
    # a cascade from deleting a whole client would otherwise try to write log
    # rows pointing at the client being deleted.
    if raw:
        return
    from .updates import record_saved
    record_saved(instance, created)
    instance.remember_tracked()
