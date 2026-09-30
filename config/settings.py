"""
Django settings for Livo OS.
Runs on SQLite locally; set DATABASE_URL in .env to use PostgreSQL.
On Vercel (detected via its VERCEL env var) without DATABASE_URL it runs as a
self-contained demo on a bundled SQLite file — see VERCEL.md.
"""
from pathlib import Path
import os
import shutil
import tempfile
import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _flag(name, default):
    """Env booleans. Accepts True/true/1/yes so a .env typo doesn't silently
    turn a security switch off."""
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name, default):
    try:
        return int(os.getenv(name, "").strip())
    except ValueError:
        return default


# Vercel sets VERCEL=1 in every build and function. Everything below that
# differs on Vercel keys off this one flag, so the VPS and local dev are untouched.
ON_VERCEL = bool(os.getenv("VERCEL"))

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-change-me")
# Off by default on Vercel: a deploy that forgot to set DEBUG must not publish
# tracebacks and settings to the internet.
DEBUG = _flag("DEBUG", not ON_VERCEL)
if not DEBUG and SECRET_KEY == "dev-insecure-change-me":
    raise ImproperlyConfigured(
        "SECRET_KEY is unset. Set it in the environment (Vercel: Project → "
        "Settings → Environment Variables) before running with DEBUG off.")

# ALLOWED_HOSTS was ["*"], which accepts any Host header — that makes
# password-reset links forgeable, because Django builds them from the request's
# host. Wide open is only tolerable while DEBUG is on and nothing is public.
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]
if not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"] if DEBUG else []
# Vercel's own hostnames for this deployment: the per-deploy URL, the branch
# URL and the production *.vercel.app alias. Custom domains still go in
# ALLOWED_HOSTS. Named one by one rather than ".vercel.app", which would accept
# any project's hostname in a Host header.
VERCEL_HOSTS = [
    h for h in (os.getenv("VERCEL_URL"), os.getenv("VERCEL_BRANCH_URL"),
                os.getenv("VERCEL_PROJECT_PRODUCTION_URL"))
    if h
]
ALLOWED_HOSTS += [h for h in VERCEL_HOSTS if h not in ALLOWED_HOSTS]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # local apps
    "core",
    "accounts",
    "clients",
    "projects",
    "documents",
    "ai_engine",
    "finance",
    "employees",
    "analytics",
    "resource_planner",
    # NOT "calendar" — the project root is on sys.path, so an app of that name
    # would shadow the stdlib module `projects.models` imports.
    "calendar_hub",
    "client_calendar",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Right after SecurityMiddleware, per WhiteNoise's docs, so static files
    # skip everything below. The only static server on Vercel.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # After auth and sessions: it resolves the viewer's workspace scope from
    # both. Everything downstream — views, selectors, chart builders — reads
    # that scope through core.tenancy rather than being handed a request.
    "core.tenancy.WorkspaceMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.branding",
                "core.context_processors.workspace",
                "core.context_processors.uploads",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# SQLite by default; PostgreSQL when DATABASE_URL is set.
# Demo mode: on Vercel with no DATABASE_URL, run on demo/livo_demo.sqlite3,
# built by `python manage.py build_demo_db`. The deployment is read-only
# outside /tmp, so each function instance copies the file there on start.
# Writes work, but belong to that instance and reset when Vercel recycles it —
# fine for a demo, wrong for real use (set DATABASE_URL for that).
DEMO_MODE = ON_VERCEL and not os.getenv("DATABASE_URL")
DEMO_DB_SOURCE = BASE_DIR / "demo" / "livo_demo.sqlite3"
if DEMO_MODE:
    if not DEMO_DB_SOURCE.exists():
        raise ImproperlyConfigured(
            "Demo mode needs demo/livo_demo.sqlite3 — run "
            "`python manage.py build_demo_db` and deploy again, or set DATABASE_URL.")
    _demo_db = Path(tempfile.gettempdir()) / "livo_demo.sqlite3"
    if not _demo_db.exists():
        shutil.copyfile(DEMO_DB_SOURCE, _demo_db)
    os.environ["DATABASE_URL"] = f"sqlite:///{_demo_db.as_posix()}"
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        # Serverless instances come and go; a shorter reuse window plus a
        # health check keeps a frozen instance from waking on a dead socket.
        conn_max_age=60 if ON_VERCEL else 600,
        conn_health_checks=ON_VERCEL,
    )
}

# In demo mode sessions live in a signed cookie, not in the per-instance
# database, so a sign-in survives Vercel moving the visitor to another instance.
if DEMO_MODE:
    SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": _int("PASSWORD_MIN_LENGTH", 10)}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# app.css was served under a stable URL, so a stylesheet change reached anyone
# who had already loaded the app only when their browser happened to revalidate
# — new markup rendering against old CSS, which looks like a broken page rather
# than a caching problem. Hashing the filename in production makes each build's
# URL new, so there is nothing to invalidate. Left off under DEBUG: the manifest
# only exists after collectstatic, and requiring that in dev would turn every
# edit into a build step.
if ON_VERCEL:
    # No collectstatic step runs on Vercel, so there is no manifest to hash
    # against. WhiteNoise serves straight from the app and static/ directories
    # via the finders instead, with a short cache lifetime standing in for
    # hashed filenames: a CSS change reaches every browser within a minute.
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
    WHITENOISE_USE_FINDERS = True
    WHITENOISE_MAX_AGE = 60
elif not DEBUG:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
        },
    }

MEDIA_URL = "media/"
# /tmp is the only writable path on Vercel, and it is per-instance and wiped on
# cold start: uploads there do not persist. See "Uploads" in VERCEL.md.
MEDIA_ROOT = Path(tempfile.gettempdir()) / "media" if ON_VERCEL else BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

# --- email ---
# Console backend while developing so the OTP and reset links land in the
# runserver output instead of needing real SMTP credentials. Set EMAIL_HOST_USER
# (and an app password) in .env to send for real, in dev or production.
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = _int("EMAIL_PORT", 587)
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "").strip()
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = _flag("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = _flag("EMAIL_USE_SSL", False)
EMAIL_TIMEOUT = _int("EMAIL_TIMEOUT", 20)
EMAIL_SUBJECT_PREFIX = ""
DEFAULT_FROM_EMAIL = os.getenv(
    "DEFAULT_FROM_EMAIL",
    f"Livo OS <{EMAIL_HOST_USER}>" if EMAIL_HOST_USER else "noreply@livodigital.com",
)
SERVER_EMAIL = DEFAULT_FROM_EMAIL
EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend" if EMAIL_HOST_USER
    else "django.core.mail.backends.console.EmailBackend",
)

# Where this install lives, e.g. https://za-an.com. Emails triggered by a web
# request take the host from the request; the ones sent by a cron job have no
# request to ask, so they fall back to this. Left blank, those emails simply go
# out without a link rather than with a broken one.
SITE_URL = os.getenv("SITE_URL", "").strip().rstrip("/")

# --- notification emails ---
# The in-app bell only reaches someone already looking at the app. These three
# events are the ones that have to reach a person who isn't: work handed to
# them, a project opening up to them, and a meeting they're expected at. Each
# can be turned off independently, at which point the bell is the only channel.
#
# Being handed work.
TASK_ASSIGNMENT_EMAILS = _flag("TASK_ASSIGNMENT_EMAILS", True)
# Being added to a project. Membership is what makes a project visible at all
# (projects/access.py), so this is the moment a whole area of the app appears.
PROJECT_MEMBER_EMAILS = _flag("PROJECT_MEMBER_EMAILS", True)
# Meeting invitations, changes and cancellations. Each carries an .ics, so the
# appointment lands in the recipient's own Google/Outlook/Apple calendar.
CALENDAR_INVITE_EMAILS = _flag("CALENDAR_INVITE_EMAILS", True)

# --- login security ---
# Every sign-in is username + password, then a one-time code shown on the
# verification screen (it is not emailed). Turning this off skips that screen.
OTP_LOGIN_REQUIRED = _flag("OTP_LOGIN_REQUIRED", True)
OTP_LENGTH = _int("OTP_LENGTH", 6)
OTP_TTL_SECONDS = _int("OTP_TTL_SECONDS", 10 * 60)
OTP_MAX_ATTEMPTS = _int("OTP_MAX_ATTEMPTS", 5)
OTP_RESEND_COOLDOWN_SECONDS = _int("OTP_RESEND_COOLDOWN_SECONDS", 60)
# How long the half-authenticated "password accepted, code pending" state lives.
OTP_PENDING_TTL_SECONDS = _int("OTP_PENDING_TTL_SECONDS", 15 * 60)

# Failed password attempts before the username (and the source IP) are frozen.
LOGIN_MAX_FAILURES = _int("LOGIN_MAX_FAILURES", 5)
LOGIN_FAILURE_WINDOW_SECONDS = _int("LOGIN_FAILURE_WINDOW_SECONDS", 15 * 60)
LOGIN_LOCKOUT_SECONDS = _int("LOGIN_LOCKOUT_SECONDS", 15 * 60)
# Reverse-proxy hop count. 0 = trust REMOTE_ADDR only. Set to 1 behind one nginx
# so throttling keys off the real client, not the proxy.
TRUSTED_PROXY_DEPTH = _int("TRUSTED_PROXY_DEPTH", 0)

PASSWORD_RESET_TIMEOUT = _int("PASSWORD_RESET_TIMEOUT", 60 * 60)  # 1 hour

# --- session & transport hardening ---
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = _int("SESSION_COOKIE_AGE", 12 * 60 * 60)
SESSION_SAVE_EVERY_REQUEST = True  # sliding expiry: idle sessions die, active ones don't
CSRF_COOKIE_HTTPONLY = False  # the CSRF token is read from the DOM, not JS, but keep SameSite
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# HTTPS-only switches. They must stay off over plain HTTP or the app locks
# itself out (secure cookies are never sent, so login can't persist).
# Vercel is HTTPS-only and terminates TLS at its edge, so on Vercel these
# default on and the forwarded-proto header is trusted.
SECURE_SSL_REDIRECT = _flag("SECURE_SSL_REDIRECT", False)
SESSION_COOKIE_SECURE = _flag("SESSION_COOKIE_SECURE", ON_VERCEL)
CSRF_COOKIE_SECURE = _flag("CSRF_COOKIE_SECURE", ON_VERCEL)
SECURE_HSTS_SECONDS = _int("SECURE_HSTS_SECONDS", 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = _flag("SECURE_HSTS_INCLUDE_SUBDOMAINS", True)
SECURE_HSTS_PRELOAD = _flag("SECURE_HSTS_PRELOAD", False)
if _flag("BEHIND_TLS_PROXY", ON_VERCEL):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]
CSRF_TRUSTED_ORIGINS += [f"https://{h}" for h in VERCEL_HOSTS
                         if f"https://{h}" not in CSRF_TRUSTED_ORIGINS]
# A readable "this page expired" instead of Django's bare 403, plus a log line
# naming the reason. See core/csrf.py.
CSRF_FAILURE_VIEW = "core.csrf.csrf_failure"

# --- uploads ---
# Hard ceiling per file. Anything larger is refused with a prompt to share a
# Drive link instead, so the VPS disk isn't the dumping ground for raw exports.
MAX_UPLOAD_BYTES = _int("MAX_UPLOAD_MB", 5) * 1024 * 1024
# Images are re-encoded on the way in: capped to this longest edge and saved at
# IMAGE_QUALITY, which is what keeps a 4 MB phone photo from staying 4 MB.
IMAGE_MAX_EDGE = _int("IMAGE_MAX_EDGE", 2000)
IMAGE_QUALITY = _int("IMAGE_QUALITY", 80)

# --- document TEMPLATE page images are the one deliberate exception ---
# The three template pages (cover, content background, back) are full-bleed A4
# print designs, not attachments. A4 at 300 DPI is 2480x3508, so the general
# rules above are actively wrong for them twice over: 5 MB refuses a normal
# export outright, and downscaling to a 2000px edge silently degrades the
# letterhead every future document is generated on -- a loss nobody sees until
# a client is looking at the PDF.
#
# Both ceilings are therefore lifted here, and both stay configurable so they
# can be put back without a code change:
#   TEMPLATE_UPLOAD_MAX_MB=0    no size ceiling      (any other number = MB)
#   TEMPLATE_IMAGE_MAX_EDGE=0   store the file as-is (any other number = px)
#
# TEMPLATE_IMAGE_MAX_EDGE=0 disables re-encoding entirely, not just the
# downscale. Lifting the pixel cap alone was not enough: compress_image
# converts an opaque PNG to JPEG at IMAGE_QUALITY whatever its dimensions, so a
# print master still came back as quality-80 JPEG with banding across every
# gradient. Set it to a real pixel value to trade exactness for smaller files.
#
# This exemption reaches exactly one screen. Everything else in the app --
# documents, avatars, client logos -- keeps MAX_UPLOAD_BYTES. See
# `core.uploads.uncapped_uploads` and `documents.views_templates`.
TEMPLATE_UPLOAD_MAX_BYTES = _int("TEMPLATE_UPLOAD_MAX_MB", 0) * 1024 * 1024
TEMPLATE_IMAGE_MAX_EDGE = _int("TEMPLATE_IMAGE_MAX_EDGE", 0)
# Reject anything not on this list before it touches the disk.
ALLOWED_UPLOAD_EXTENSIONS = [
    e.strip().lower().lstrip(".")
    for e in os.getenv(
        "ALLOWED_UPLOAD_EXTENSIONS",
        "pdf,doc,docx,xls,xlsx,ppt,pptx,csv,txt,rtf,odt,ods,"
        "jpg,jpeg,png,gif,webp,bmp,tiff,svg,heic,"
        "zip,mp4,mov,webm,mp3,wav,ai,psd,eps,fig,sketch,json,xml",
    ).split(",")
    if e.strip()
]
# DATA_UPLOAD_MAX_MEMORY_SIZE caps non-file POST data only — file parts are
# exempt from it, so on its own it does nothing to stop a 2 GB upload from being
# streamed to the server's temp disk first. CappedFileUploadHandler is what
# actually aborts the transfer mid-stream; it must come before the built-ins.
FILE_UPLOAD_HANDLERS = [
    "core.uploads.CappedFileUploadHandler",
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
    "django.core.files.uploadhandler.TemporaryFileUploadHandler",
]
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000
FILE_UPLOAD_PERMISSIONS = 0o644

# --- AI engine (Groq Cloud) ---
# GROQ_API_KEYS accepts a comma-separated list; each key is tried in order
# when the previous one fails. With no keys, offline placeholders are used.
GROQ_API_KEYS = [
    k.strip()
    for k in os.getenv("GROQ_API_KEYS", os.getenv("GROQ_API_KEY", "")).split(",")
    if k.strip()
]
# Groq retires hosted models on its own schedule and the endpoint answers a
# retired id with a 404, not a fallback — which is silent here, because
# `ai_engine.services` is built to degrade to a placeholder rather than 500 a
# document. `llama-3.3-70b-versatile` was that 404 until Aug 2026. If AI
# output starts coming back as placeholders, check this against
# https://api.groq.com/openai/v1/models before looking anywhere else.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
