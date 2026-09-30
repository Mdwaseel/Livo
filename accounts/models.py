import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

# The RBAC actions. Order here drives the matrix UI columns and the
# RolePermission boolean field names (can_<action>).
ACTIONS = (
    "view", "create", "edit", "delete", "approve",
    "export", "import", "assign", "archive", "restore",
    # "view" answers "may you open this module at all"; "view_all" answers
    # "which rows". Without it, projects are limited to the ones you are a
    # member of and tasks to the ones that are yours — see projects/access.py.
    "view_all",
)

# Column headings for the permission matrix. Only actions whose name doesn't
# read well capitalised need an entry; the rest fall back to capfirst.
ACTION_LABELS = {"view_all": "View all"}


class Department(models.Model):
    """Org unit (Delivery, Sales, Accounts, HR…). Employees hang off this."""
    name = models.CharField(max_length=120, unique=True)
    head = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="headed_departments",
    )
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Designation(models.Model):
    """A job title, optionally scoped to a department, with a seniority level."""
    name = models.CharField(max_length=120)
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="designations",
    )
    level = models.PositiveIntegerField(default=1, help_text="Seniority: higher = senior")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "-level", "name"]

    def __str__(self):
        return f"{self.name}{f' · {self.department.name}' if self.department else ''}"


class Module(models.Model):
    """Catalog of app modules that permissions attach to. Adding a module here
    (plus seed defaults) is all a new feature needs to become permission-aware."""
    key = models.SlugField(max_length=50, unique=True)
    label = models.CharField(max_length=100)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "label"]

    def __str__(self):
        return self.label


class Role(models.Model):
    """A named permission set. is_system roles (Super Admin, Manager) cannot be
    renamed or deleted from the UI."""
    name = models.CharField(max_length=80, unique=True)
    description = models.TextField(blank=True)
    is_system = models.BooleanField(default=False)
    priority = models.PositiveIntegerField(
        default=100, help_text="Lower = more senior; used only for display order")

    class Meta:
        ordering = ["priority", "name"]

    def __str__(self):
        return self.name


class RolePermission(models.Model):
    """One row per (role, module): which of the ACTIONS the role may do."""
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="permissions")
    module = models.ForeignKey(Module, on_delete=models.CASCADE,
                               related_name="role_permissions")
    can_view = models.BooleanField(default=False)
    can_create = models.BooleanField(default=False)
    can_edit = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)
    can_approve = models.BooleanField(default=False)
    can_export = models.BooleanField(default=False)
    can_import = models.BooleanField(default=False)
    can_assign = models.BooleanField(default=False)
    can_archive = models.BooleanField(default=False)
    can_restore = models.BooleanField(default=False)
    can_view_all = models.BooleanField(default=False)

    class Meta:
        unique_together = [("role", "module")]

    def __str__(self):
        return f"{self.role.name} · {self.module.key}"

    def granted_actions(self):
        return {a for a in ACTIONS if getattr(self, f"can_{a}")}


class User(AbstractUser):
    """Custom user. `role` (the old hardcoded CharField) is DEPRECATED — kept
    only until every reference is gone; permissions now come from primary_role
    + extra_roles via accounts.permissions.has_perm.

    `role` deliberately has NO default. It used to default to OWNER, which the
    legacy-role signal translated into a Super Admin primary_role — so every
    user created without naming a role (Django admin's add form, a bare
    create_user, any future signup) silently became a super admin. Blank now
    means "no legacy role", and the signal grants nothing.
    """

    class Role(models.TextChoices):
        OWNER = "OWNER", "Business Owner"
        SALES = "SALES", "Sales"
        PM = "PM", "Project Manager"
        DEVELOPER = "DEV", "Developer"
        ACCOUNTS = "ACCT", "Accounts"

    role = models.CharField(  # DEPRECATED — see class docstring
        max_length=10, choices=Role.choices, blank=True, default="")
    phone = models.CharField(max_length=40, blank=True)

    # --- tenancy ---
    # The partition this person lives in. Unset reads as the home workspace —
    # our own staff. A user in a partner workspace sees that workspace and
    # nothing else, however senior their role. See core.tenancy.
    workspace = models.ForeignKey(
        "core.Workspace", on_delete=models.PROTECT, null=True, blank=True,
        related_name="users",
        help_text="Which partition this person works in. Leave unset for "
                  "your own agency's staff.")

    # --- org structure ---
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="members")
    designation = models.ForeignKey(
        Designation, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="holders")
    reports_to = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reports")
    employee_code = models.CharField(max_length=30, blank=True)

    # --- dynamic RBAC ---
    # String refs because the deprecated inner TextChoices class shadows the
    # module-level Role model inside this class body.
    primary_role = models.ForeignKey(
        "accounts.Role", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="primary_users")
    extra_roles = models.ManyToManyField(
        "accounts.Role", blank=True, related_name="extra_users")

    def __str__(self):
        return self.get_full_name() or self.username


# ---------------------------------------------------------------------------
# login security
# ---------------------------------------------------------------------------

class LoginOTP(models.Model):
    """A one-time code emailed after the password step.

    Only a keyed hash of the code is stored. A dump of this table — a leaked
    backup, a read-only SQL injection — must not hand anyone a working code, and
    it can't, because deriving the digest needs SECRET_KEY.

    `session_key` binds the code to the browser that submitted the password, so
    a code seen elsewhere can't be replayed without also holding that session
    cookie. It stores `security.session_binding()`, a nonce kept in the
    session, rather than the session's own key.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="login_otps")
    code_hash = models.CharField(max_length=64)
    session_key = models.CharField(max_length=64, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self):
        return f"OTP for {self.user} ({'used' if self.consumed_at else 'live'})"

    # --- code handling ---

    @staticmethod
    def generate_code(length=None):
        length = length or getattr(settings, "OTP_LENGTH", 6)
        # randbelow, not random.randint: this is a credential.
        return "".join(str(secrets.randbelow(10)) for _ in range(length))

    @staticmethod
    def hash_code(code):
        # sha256, not salted_hmac's sha1 default; the 64-char digest is what
        # code_hash's max_length is sized for.
        return salted_hmac("livo.login-otp", code,
                           algorithm="sha256").hexdigest()

    def matches(self, code):
        return constant_time_compare(self.code_hash, self.hash_code((code or "").strip()))

    # --- state ---

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def is_usable(self):
        return (self.consumed_at is None
                and not self.is_expired
                and self.attempts < getattr(settings, "OTP_MAX_ATTEMPTS", 5))

    def seconds_until_resend(self):
        cooldown = getattr(settings, "OTP_RESEND_COOLDOWN_SECONDS", 60)
        elapsed = (timezone.now() - self.created_at).total_seconds()
        return max(0, int(cooldown - elapsed))

    @classmethod
    def issue(cls, user, *, session_key="", ip_address=None):
        """Invalidate any outstanding codes and mint a fresh one.

        Returns (instance, plaintext_code) — the plaintext exists only long
        enough to be put in an email and is never written down.
        """
        cls.objects.filter(user=user, consumed_at__isnull=True).update(
            consumed_at=timezone.now())
        code = cls.generate_code()
        ttl = getattr(settings, "OTP_TTL_SECONDS", 600)
        otp = cls.objects.create(
            user=user,
            code_hash=cls.hash_code(code),
            session_key=session_key or "",
            ip_address=ip_address,
            expires_at=timezone.now() + timedelta(seconds=ttl),
        )
        return otp, code

    @classmethod
    def purge_stale(cls, older_than_days=7):
        cutoff = timezone.now() - timedelta(days=older_than_days)
        cls.objects.filter(created_at__lt=cutoff).delete()


class RateLimit(models.Model):
    """Failure counter for one key ("pw:alice", "ip:10.0.0.4", "reset:bob").

    In the database rather than the cache deliberately: the default cache is
    per-process, so with more than one gunicorn worker an attacker would get
    LOGIN_MAX_FAILURES attempts *per worker*, and every restart would wipe the
    lockout. A row survives both.
    """

    key = models.CharField(max_length=190, unique=True)
    failures = models.PositiveIntegerField(default=0)
    first_failure_at = models.DateTimeField(auto_now_add=True)
    last_failure_at = models.DateTimeField(auto_now=True)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["locked_until"])]

    def __str__(self):
        return f"{self.key} · {self.failures} failure(s)"

    @property
    def is_locked(self):
        return bool(self.locked_until and timezone.now() < self.locked_until)

    def seconds_remaining(self):
        if not self.is_locked:
            return 0
        return int((self.locked_until - timezone.now()).total_seconds())
