"""Brute-force throttling, client-IP resolution, and the sign-in code.

Kept out of views.py so the login flow reads as flow, and so the throttle can be
reused by anything else that guards a credential (the password-reset request
form already does).
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import LoginOTP, RateLimit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# who is calling
# ---------------------------------------------------------------------------

def client_ip(request):
    """The caller's address, honouring X-Forwarded-For only as far as
    TRUSTED_PROXY_DEPTH allows.

    Blindly trusting the leftmost X-Forwarded-For entry is the classic way to
    make IP throttling useless: the header is client-supplied, so an attacker
    just sends a different fake value per request and never trips the counter.
    We count in from the right — those entries were appended by our own proxies
    — and fall back to REMOTE_ADDR when the depth is 0.
    """
    depth = getattr(settings, "TRUSTED_PROXY_DEPTH", 0)
    if depth > 0:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        chain = [part.strip() for part in forwarded.split(",") if part.strip()]
        if len(chain) >= depth:
            return chain[-depth]
    return request.META.get("REMOTE_ADDR") or None


# ---------------------------------------------------------------------------
# throttling
# ---------------------------------------------------------------------------

def _window():
    return timedelta(seconds=getattr(settings, "LOGIN_FAILURE_WINDOW_SECONDS", 900))


def lockout_seconds(*keys):
    """Longest remaining lockout across the given keys; 0 if none are locked."""
    now = timezone.now()
    remaining = 0
    for bucket in RateLimit.objects.filter(key__in=[k for k in keys if k]):
        if bucket.locked_until and bucket.locked_until > now:
            remaining = max(remaining, int((bucket.locked_until - now).total_seconds()))
    return remaining


def register_failure(*keys):
    """Count one failure against each key, locking whichever crosses the limit.

    Runs in a transaction with `select_for_update` so two simultaneous guesses
    can't both read `failures = 4` and each write back 5.
    """
    limit = getattr(settings, "LOGIN_MAX_FAILURES", 5)
    lock_for = timedelta(seconds=getattr(settings, "LOGIN_LOCKOUT_SECONDS", 900))
    now = timezone.now()
    locked = 0
    for key in [k for k in keys if k]:
        with transaction.atomic():
            bucket, created = RateLimit.objects.get_or_create(key=key)
            if not created:
                bucket = RateLimit.objects.select_for_update().get(pk=bucket.pk)
            # A burst that started long ago isn't evidence of an attack now.
            if not created and bucket.first_failure_at < now - _window():
                bucket.failures = 0
                bucket.first_failure_at = now
                bucket.locked_until = None
            bucket.failures += 1
            if bucket.failures >= limit:
                bucket.locked_until = now + lock_for
                locked = max(locked, int(lock_for.total_seconds()))
            bucket.save()
    return locked


def clear_failures(*keys):
    RateLimit.objects.filter(key__in=[k for k in keys if k]).delete()


def user_key(username):
    # Lower-cased so "Admin" and "admin" share one counter instead of doubling
    # the allowance, and truncated to fit the column.
    return f"pw:{(username or '').strip().lower()[:150]}"


def ip_key(ip):
    return f"ip:{ip}" if ip else ""


def otp_key(user_id):
    return f"otp:{user_id}"


def reset_key(ip):
    return f"reset:{ip}" if ip else ""


def humanise_seconds(seconds):
    if seconds <= 90:
        return f"{max(1, seconds)} second{'s' if seconds != 1 else ''}"
    return f"{round(seconds / 60)} minutes"


# ---------------------------------------------------------------------------
# the sign-in code
# ---------------------------------------------------------------------------

SESSION_BINDING = "otp_binding"


def session_binding(request):
    """A random value stored in this browser's session, that a login code is
    bound to.

    Not `session.session_key`: with signed-cookie sessions (the Vercel demo)
    that "key" is the signed payload itself, which changes on every write, so a
    code bound to it could never match. A nonce kept *in* the session gives the
    same guarantee with either backend: only the browser holding this session
    can present it.
    """
    value = request.session.get(SESSION_BINDING)
    if not value:
        value = request.session[SESSION_BINDING] = secrets.token_urlsafe(24)
    return value


def issue_login_code(request, user):
    """Mint a code for `user` and return it in plain text.

    Nothing is emailed: the code is shown on the verification screen instead.
    It is still hashed at rest, bound to this browser session, short-lived and
    attempt-capped by LoginOTP, so the second step keeps its shape even though
    it no longer proves control of an inbox.
    """
    otp, code = LoginOTP.issue(
        user,
        session_key=session_binding(request),
        ip_address=client_ip(request),
    )
    logger.info("Login code issued for user %s", user.pk)
    return code
