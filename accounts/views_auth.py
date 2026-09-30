"""Sign-in, on-screen one-time code, sign-out, and password reset.

The login is two steps. Step one takes username + password but does **not** log
anyone in; on success it parks a "pending" marker in the session and mints a
code, which the verification screen displays (nothing is emailed). Step two exchanges that code for a real session. Between the two the
visitor is not authenticated — `request.user` is still anonymous, so nothing
behind @login_required is reachable with only a stolen password.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth import views as auth_views
from django.shortcuts import redirect, render, resolve_url
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from core.models import log_activity

from .forms import BrandedPasswordResetForm, BrandedSetPasswordForm, LoginForm, OTPForm
from .models import LoginOTP
from .security import (SESSION_BINDING, clear_failures, client_ip,
                       humanise_seconds, ip_key, issue_login_code, lockout_seconds,
                       otp_key, register_failure, reset_key, session_binding,
                       user_key)

logger = logging.getLogger(__name__)
User = get_user_model()

PENDING_USER = "otp_pending_user"
PENDING_BACKEND = "otp_pending_backend"
PENDING_SINCE = "otp_pending_since"
PENDING_NEXT = "otp_pending_next"
PENDING_CODE = "otp_pending_code"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _safe_next(request, raw):
    """Only redirect to our own host. Without this check, `?next=//evil.example`
    turns the login page into an open redirect that lends us its credibility for
    a phishing hop."""
    if raw and url_has_allowed_host_and_scheme(
            raw, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return raw
    return resolve_url(settings.LOGIN_REDIRECT_URL)


def _clear_pending(session):
    for key in (PENDING_USER, PENDING_BACKEND, PENDING_SINCE, PENDING_NEXT,
                PENDING_CODE, SESSION_BINDING):
        session.pop(key, None)


def _pending_user(request):
    """The half-authenticated user, or None when there isn't a live one."""
    user_id = request.session.get(PENDING_USER)
    started = request.session.get(PENDING_SINCE)
    if not user_id or not started:
        return None
    age = timezone.now().timestamp() - started
    if age > getattr(settings, "OTP_PENDING_TTL_SECONDS", 900):
        _clear_pending(request.session)
        return None
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if not user:
        # Deactivated between the password step and the code step.
        _clear_pending(request.session)
    return user


def _begin_otp(request, user, next_url):
    """Park the pending state and mint the code the next screen shows."""
    # Fresh session key before the pending state is written, so a cookie an
    # attacker planted on this browser earlier can't be the one the code is
    # bound to. login() cycles it again on success.
    request.session.cycle_key()
    request.session[PENDING_USER] = user.pk
    request.session[PENDING_BACKEND] = getattr(user, "backend", "")
    request.session[PENDING_SINCE] = timezone.now().timestamp()
    request.session[PENDING_NEXT] = next_url
    request.session[PENDING_CODE] = issue_login_code(request, user)


# ---------------------------------------------------------------------------
# step 1 — password
# ---------------------------------------------------------------------------

@never_cache
@csrf_protect
@sensitive_post_parameters("password")
def login_view(request):
    if request.user.is_authenticated:
        return redirect(_safe_next(request, request.GET.get("next")))

    next_url = _safe_next(request, request.GET.get("next") or request.POST.get("next"))
    form = LoginForm(request)
    locked_message = ""

    if request.method == "POST":
        ip = client_ip(request)
        username = request.POST.get("username", "")
        keys = (user_key(username), ip_key(ip))

        wait = lockout_seconds(*keys)
        if wait:
            # Don't even check the password — that is the whole point of a
            # lockout, and checking would leak timing about the account.
            locked_message = (
                "Too many failed attempts. Try again in "
                f"{humanise_seconds(wait)}, or reset your password.")
            logger.warning("Blocked login attempt for %r from %s (locked)", username, ip)
        else:
            form = LoginForm(request, data=request.POST)
            if form.is_valid():
                user = form.get_user()
                clear_failures(*keys)

                if not getattr(settings, "OTP_LOGIN_REQUIRED", True):
                    login(request, user)
                    log_activity(user, "signed in", "password only (OTP disabled)")
                    return redirect(next_url)

                _begin_otp(request, user, next_url)
                return redirect(reverse("accounts:login_otp"))
            else:
                wait = register_failure(*keys)
                logger.warning("Failed login for %r from %s", username, ip)
                if wait:
                    locked_message = (
                        "Too many failed attempts. This account is locked for "
                        f"{humanise_seconds(wait)}.")

    return render(request, "accounts/login.html", {
        "form": form,
        "next": next_url,
        "locked_message": locked_message,
    })


# ---------------------------------------------------------------------------
# step 2 — the on-screen code
# ---------------------------------------------------------------------------

@never_cache
@csrf_protect
@sensitive_post_parameters("code")
def login_otp_view(request):
    if request.user.is_authenticated:
        return redirect(_safe_next(request, request.GET.get("next")))

    user = _pending_user(request)
    if not user:
        messages.error(request, "Your sign-in timed out. Please start again.")
        return redirect("accounts:login")

    form = OTPForm()
    error = ""
    ip = client_ip(request)

    if request.method == "POST":
        keys = (otp_key(user.pk), ip_key(ip))
        wait = lockout_seconds(*keys)
        if wait:
            error = f"Too many incorrect codes. Try again in {humanise_seconds(wait)}."
        else:
            form = OTPForm(request.POST)
            if form.is_valid():
                otp = (LoginOTP.objects
                       .filter(user=user, consumed_at__isnull=True)
                       .order_by("-created_at").first())
                if not otp or otp.is_expired:
                    error = "That code has expired. Get a new one below."
                elif otp.session_key and otp.session_key != session_binding(request):
                    # The code was issued to a different browser session.
                    error = "That code isn't valid for this browser. Please sign in again."
                    logger.warning("OTP session mismatch for user %s from %s", user.pk, ip)
                elif otp.attempts >= getattr(settings, "OTP_MAX_ATTEMPTS", 5):
                    error = "That code has been tried too many times. Request a new one."
                elif otp.matches(form.cleaned_data["code"]):
                    otp.consumed_at = timezone.now()
                    otp.save(update_fields=["consumed_at"])
                    clear_failures(*keys)
                    next_url = request.session.get(PENDING_NEXT) or resolve_url(
                        settings.LOGIN_REDIRECT_URL)
                    backend = request.session.get(PENDING_BACKEND) or None
                    _clear_pending(request.session)
                    # login() rotates the session key, so the pre-verification
                    # session id an attacker might have planted stops working.
                    login(request, user, backend=backend)
                    LoginOTP.purge_stale()
                    log_activity(user, "signed in", f"verified by sign-in code · {ip or '?'}")
                    return redirect(_safe_next(request, next_url))
                else:
                    otp.attempts += 1
                    otp.save(update_fields=["attempts"])
                    remaining = max(
                        0, getattr(settings, "OTP_MAX_ATTEMPTS", 5) - otp.attempts)
                    locked_for = register_failure(*keys)
                    error = (
                        f"Too many incorrect codes. Try again in {humanise_seconds(locked_for)}."
                        if locked_for else
                        f"That code isn't right. {remaining} attempt"
                        f"{'s' if remaining != 1 else ''} left.")
            else:
                error = "Enter the code shown above."

    latest = (LoginOTP.objects.filter(user=user, consumed_at__isnull=True)
              .order_by("-created_at").first())
    return render(request, "accounts/login_otp.html", {
        "form": form,
        "error": error,
        # Only the live code is shown: once it is used, expires or the
        # pending sign-in is cleared, the session no longer holds it.
        "display_code": (request.session.get(PENDING_CODE, "")
                         if latest and not latest.is_expired else ""),
        "resend_in": latest.seconds_until_resend() if latest else 0,
        "minutes": max(1, round(getattr(settings, "OTP_TTL_SECONDS", 600) / 60)),
    })


@never_cache
@csrf_protect
@require_POST
def login_otp_resend(request):
    user = _pending_user(request)
    if not user:
        messages.error(request, "Your sign-in timed out. Please start again.")
        return redirect("accounts:login")

    latest = (LoginOTP.objects.filter(user=user, consumed_at__isnull=True)
              .order_by("-created_at").first())
    if latest and latest.seconds_until_resend() > 0:
        messages.error(request, "Please wait a moment before requesting another code.")
        return redirect("accounts:login_otp")

    request.session[PENDING_CODE] = issue_login_code(request, user)
    messages.success(request, "Here's a new code.")
    return redirect("accounts:login_otp")


@never_cache
@require_POST
def logout_view(request):
    """Sign out, and also drop any half-finished sign-in."""
    _clear_pending(request.session)
    logout(request)
    messages.success(request, "You've been signed out.")
    return redirect("accounts:login")


# ---------------------------------------------------------------------------
# password reset
# ---------------------------------------------------------------------------

class PasswordResetRequestView(auth_views.PasswordResetView):
    """"Email me a reset link".

    Always renders the same confirmation page, whether or not the address is
    known — otherwise the form answers "does this person have an account here?"
    for anyone who asks.
    """

    template_name = "accounts/password_reset_form.html"
    email_template_name = "accounts/email/password_reset.txt"
    html_email_template_name = "accounts/email/password_reset.html"
    subject_template_name = "accounts/email/password_reset_subject.txt"
    form_class = BrandedPasswordResetForm
    success_url = reverse_lazy("accounts:password_reset_done")

    def form_valid(self, form):
        ip = client_ip(self.request)
        wait = lockout_seconds(reset_key(ip))
        if wait:
            logger.warning("Reset request throttled from %s", ip)
            return redirect(self.success_url)  # same page, no mail sent
        register_failure(reset_key(ip))  # every request counts, success or not
        try:
            response = super().form_valid(form)
        except Exception:
            logger.exception("Password reset email failed for %r",
                             form.cleaned_data.get("email"))
            return redirect(self.success_url)
        logger.info("Password reset requested from %s", ip)
        return response


class PasswordResetSentView(auth_views.PasswordResetDoneView):
    template_name = "accounts/password_reset_done.html"


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    """Behind the emailed link. The token is single-use — Django invalidates it
    once the password changes, because the hash it is built from includes the
    old password."""

    template_name = "accounts/password_reset_confirm.html"
    form_class = BrandedSetPasswordForm
    success_url = reverse_lazy("accounts:password_reset_complete")
    post_reset_login = False  # a new password still has to pass the OTP step

    def form_valid(self, form):
        response = super().form_valid(form)
        # Any code minted before the reset is void: if someone else triggered
        # this, their pending sign-in must not survive it.
        LoginOTP.objects.filter(user=form.user, consumed_at__isnull=True).update(
            consumed_at=timezone.now())
        clear_failures(user_key(form.user.get_username()))
        log_activity(form.user, "reset password", "via emailed link")
        return response


class PasswordResetFinishedView(auth_views.PasswordResetCompleteView):
    template_name = "accounts/password_reset_complete.html"
