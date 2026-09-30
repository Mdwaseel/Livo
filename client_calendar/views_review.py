"""The pages a client's reviewer opens from an approval email.

No login, and not the calendar's shared link either: the URL carries one
reviewer's token, and before anything is shown the browser has to hold a
signed cookie that says this reviewer proved they own their inbox. Getting that
cookie means typing a code emailed to that inbox. So a link that is forwarded,
pasted into a group chat or lifted from a shared mailbox shows the next person
a masked address and a "send me a code" button — never the work.

Everything is looked up from the token, never from a parameter: another
reviewer's request, another client's file, an archived project — each is a 404
here, not a hidden element in a template.
"""
import mimetypes
import re

from django.conf import settings
from django.core import signing
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.security import (clear_failures, client_ip, lockout_seconds,
                               register_failure)

from . import approvals, updates
from .models import (ApprovalAsset, ApprovalEvent, ApprovalRequest, ClientCalendarLink,
                     ClientReviewer)

VERIFY_STATES = {"sent", "failed", "wrong", "expired", "locked", "throttled", "cooldown", "stale"}


# ---------------------------------------------------------------------------
# forms without Django's CSRF cookie
# ---------------------------------------------------------------------------
#
# These pages are opened from an email, and clients open emails in whatever the
# mail app hands them: Gmail's and Outlook's in-app browsers, WhatsApp's, older
# Safari, a corporate link scanner, a phone with privacy extensions. Django's
# CSRF check needs a cookie *and* an Origin or Referer header, and enough of
# those browsers drop one that clients were meeting a bare "403 Forbidden" on
# the very first button.
#
# So these forms are exempt from that check and carry their own proof instead:
# a signed key, issued for this reviewer when the page was rendered, that needs
# no cookie and no header. Forgery is still pointless — a cross-site post would
# need the reviewer's secret URL, and the "verified device" cookie that every
# decision requires is SameSite=Lax, so a cross-site post never carries it.

FORM_SALT = "livo.client-review-form"


def form_key(reviewer):
    return signing.dumps({"r": reviewer.pk}, salt=FORM_SALT)


def form_key_ok(request, reviewer):
    try:
        payload = signing.loads(request.POST.get("form_key", ""), salt=FORM_SALT,
                                max_age=7 * 24 * 60 * 60)
    except signing.BadSignature:  # also covers SignatureExpired
        return False
    return isinstance(payload, dict) and payload.get("r") == reviewer.pk


def _harden(response):
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    # same-origin, not the calendar's no-referrer. These pages post forms, and
    # under no-referrer browsers send `Origin: null`, which Django's CSRF check
    # rightly refuses — the reviewer could never ask for a code or approve.
    # same-origin still sends nothing when they click out to a Google Doc or
    # Figma link, so the token doesn't leak to other sites either way.
    response["Referrer-Policy"] = "same-origin"
    response["Cache-Control"] = "private, no-store"
    return response


def _gate(request, token):
    """(reviewer, None) when the token is live, else (None, a response)."""
    reviewer = (ClientReviewer.objects.select_related("client")
                .filter(token=token).first())
    if reviewer is None:
        return None, _unavailable(request, "missing", 404)
    if not reviewer.is_active or reviewer.client.is_archived:
        return None, _unavailable(request, "off", 410)
    return reviewer, None


def _unavailable(request, reason, status):
    return _harden(render(request, "client_calendar/review/unavailable.html",
                          {"reason": reason}, status=status))


def _requests_for(reviewer):
    return (ApprovalRequest.objects
            .filter(recipients__reviewer=reviewer,
                    activity__project__client=reviewer.client,
                    activity__project__is_archived=False)
            .select_related("activity", "activity__project", "decided_by", "created_by"))


def _verify(request, reviewer):
    """The "confirm it's you" page, rendered in place of whatever was asked for,
    so the URL in the address bar is still the one to come back to."""
    state = request.GET.get("v", "")
    state = state if state in VERIFY_STATES else ""
    code_live = bool(reviewer.code_hash and reviewer.code_expires_at
                     and reviewer.code_expires_at > timezone.now()
                     and reviewer.code_attempts < getattr(
                         settings, "CLIENT_REVIEW_CODE_MAX_ATTEMPTS", 5))
    return _harden(render(request, "client_calendar/review/verify.html", {
        "reviewer": reviewer,
        "client": reviewer.client,
        "next": request.path,
        "state": state,
        # Ask for the code only while one is actually out there to type in.
        "show_code": code_live and state not in ("expired", "locked", "failed"),
        "wait": reviewer.seconds_until_resend(),
        "remember_days": getattr(settings, "CLIENT_REVIEW_REMEMBER_DAYS", 30),
        "form_key": form_key(reviewer),
    }))


def _safe_next(raw, reviewer):
    """Only ever a path under this reviewer's own prefix."""
    base = reviewer.get_absolute_url()
    path = (raw or "").split("?")[0].split("#")[0]
    if path.startswith(base) and "//" not in path and "\\" not in path:
        return path
    return base


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

@require_GET
def inbox(request, token):
    reviewer, denied = _gate(request, token)
    if denied:
        return denied
    if not approvals.device_is_trusted(request, reviewer):
        return _verify(request, reviewer)

    items = list(_requests_for(reviewer).order_by("-created_at"))
    ClientReviewer.objects.filter(pk=reviewer.pk).update(last_seen_at=timezone.now())
    pending = [item for item in items if item.is_pending]
    pending.sort(key=lambda item: (item.respond_by is None, item.respond_by or item.activity.date))
    return _harden(render(request, "client_calendar/review/inbox.html", {
        "reviewer": reviewer,
        "client": reviewer.client,
        "pending": pending,
        "decided": [item for item in items
                    if item.status in (ApprovalRequest.Status.APPROVED,
                                       ApprovalRequest.Status.CHANGES)][:30],
    }))


@require_GET
def detail(request, token, pk):
    reviewer, denied = _gate(request, token)
    if denied:
        return denied
    if not approvals.device_is_trusted(request, reviewer):
        return _verify(request, reviewer)
    approval = _requests_for(reviewer).filter(pk=pk).first()
    if approval is None:
        return _unavailable(request, "request", 404)

    approvals.record_open(approval, reviewer, ip=client_ip(request))
    siblings = _requests_for(reviewer).filter(activity=approval.activity)
    return _harden(render(request, "client_calendar/review/detail.html", {
        "reviewer": reviewer,
        "client": reviewer.client,
        "approval": approval,
        "activity": approval.activity,
        "assets": list(approval.assets.all()),
        "newer": siblings.filter(version__gt=approval.version).order_by("-version").first(),
        "earlier": list(siblings.filter(version__lt=approval.version).order_by("-version")),
        "history": list(approval.events.exclude(kind=ApprovalEvent.Kind.OPENED)
                        .select_related("reviewer", "actor")),
        "waiting_elsewhere": _requests_for(reviewer)
                             .filter(status=ApprovalRequest.Status.PENDING)
                             .exclude(pk=approval.pk).count(),
        "done": request.GET.get("done", ""),
        "form_key": form_key(reviewer),
    }))


# ---------------------------------------------------------------------------
# confirming it's them
# ---------------------------------------------------------------------------

def _keys(reviewer, ip):
    return (f"review:{reviewer.pk}", f"review-ip:{ip}" if ip else "")


@csrf_exempt  # see "forms without Django's CSRF cookie" above
@require_POST
def send_code(request, token):
    reviewer, denied = _gate(request, token)
    if denied:
        return denied
    back = _safe_next(request.POST.get("next"), reviewer)
    if not form_key_ok(request, reviewer):
        return _harden(redirect(f"{back}?v=stale"))
    if lockout_seconds(*_keys(reviewer, client_ip(request))):
        return _harden(redirect(f"{back}?v=throttled"))
    if reviewer.seconds_until_resend():
        return _harden(redirect(f"{back}?v=cooldown"))
    code = reviewer.issue_code()
    sent = approvals.send_code_email(reviewer, code, request=request)
    return _harden(redirect(f"{back}?v={'sent' if sent else 'failed'}"))


@csrf_exempt  # see "forms without Django's CSRF cookie" above
@require_POST
def check_code(request, token):
    reviewer, denied = _gate(request, token)
    if denied:
        return denied
    back = _safe_next(request.POST.get("next"), reviewer)
    if not form_key_ok(request, reviewer):
        return _harden(redirect(f"{back}?v=stale"))
    keys = _keys(reviewer, client_ip(request))
    if lockout_seconds(*keys):
        return _harden(redirect(f"{back}?v=throttled"))

    result = reviewer.check_code(request.POST.get("code", ""))
    if result == "ok":
        clear_failures(keys[0])
        response = redirect(back)
        approvals.trust_device(response, request, reviewer)
        return _harden(response)
    if result == "wrong":
        register_failure(*keys)
    return _harden(redirect(f"{back}?v={result}"))


# ---------------------------------------------------------------------------
# the answer
# ---------------------------------------------------------------------------

@csrf_exempt  # see "forms without Django's CSRF cookie" above
@require_POST
def decide(request, token, pk):
    reviewer, denied = _gate(request, token)
    if denied:
        return denied
    page = reverse("client_review:detail", args=[token, pk])
    if not approvals.device_is_trusted(request, reviewer):
        return _harden(redirect(page))
    if not form_key_ok(request, reviewer):
        return _harden(redirect(f"{page}?done=stale#decide"))
    approval = _requests_for(reviewer).filter(pk=pk).first()
    if approval is None:
        return _unavailable(request, "request", 404)

    decision = request.POST.get("decision", "")
    feedback = request.POST.get("feedback", "").strip()
    if decision not in ("approve", "changes"):
        return _harden(redirect(page))
    if decision == "changes" and not feedback:
        return _harden(redirect(f"{page}?done=needs-feedback#decide"))

    approval, recorded = approvals.decide(
        approval, reviewer, approve=decision == "approve", feedback=feedback,
        ip=client_ip(request))
    if not recorded:
        return _harden(redirect(f"{page}?done=late"))
    approvals.notify_team(approval, request=request)
    return _harden(redirect(f"{page}?done={'approved' if decision == 'approve' else 'changes'}"))


# ---------------------------------------------------------------------------
# update email preferences
# ---------------------------------------------------------------------------
#
# Reachable from the footer of every update email, so — unlike everything
# above — it does not ask for the emailed code. Unsubscribing has to work in one
# tap from any device, and all this page shows or changes is how often one
# address gets calendar emails: no work, no files, not even the full address.

def _reviewer_by_token(token):
    return ClientReviewer.objects.select_related("client").filter(token=token).first()


@csrf_exempt  # see "forms without Django's CSRF cookie" above
@require_http_methods(["GET", "POST"])
def update_prefs(request, token):
    reviewer = _reviewer_by_token(token)
    if reviewer is None:
        return _unavailable(request, "missing", 404)
    if request.method == "POST":
        frequency = request.POST.get("frequency", "")
        if not form_key_ok(request, reviewer):
            return _harden(redirect(request.path))
        if frequency in ClientReviewer.Updates.values:
            updates.set_frequency(reviewer, frequency, by_client=True)
            return _harden(redirect(f"{request.path}?saved=1"))
    link = ClientCalendarLink.objects.filter(client=reviewer.client, is_active=True).first()
    return _harden(render(request, "client_calendar/review/updates.html", {
        "reviewer": reviewer,
        "client": reviewer.client,
        "choices": [(value, label, updates.FREQUENCY_HELP[value])
                    for value, label in ClientReviewer.Updates.choices],
        "saved": request.GET.get("saved") == "1",
        "calendar_url": link.get_absolute_url() if link else "",
        "prefs_only": True,
        "form_key": form_key(reviewer),
    }))


@csrf_exempt
@require_http_methods(["GET", "POST"])
def unsubscribe(request, token):
    """RFC 8058 one-click unsubscribe target.

    POST (sent by the mail client's own Unsubscribe button, with no cookies and
    no CSRF token — hence the exemption) turns update emails off. GET, which is
    what a link scanner or a curious click sends, changes nothing and shows the
    preferences page instead.
    """
    reviewer = _reviewer_by_token(token)
    if reviewer is None:
        return HttpResponse(status=404)
    if request.method == "POST":
        updates.set_frequency(reviewer, ClientReviewer.Updates.OFF, by_client=True)
        return _harden(HttpResponse("You're unsubscribed from calendar update emails.",
                                    content_type="text/plain; charset=utf-8"))
    return _harden(redirect(reverse("client_review:updates", args=[token])))


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

INLINE = ("image", "video", "audio", "pdf")
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _ranged(request, fieldfile, content_type):
    """A 206 for a `Range:` request, or None to send the whole file.

    Safari will not play an mp4 from a server that can't answer byte ranges,
    and a reel for approval that won't play on the client's iPhone is the one
    failure this page can't afford.
    """
    match = _RANGE.match(request.META.get("HTTP_RANGE", "").strip())
    if not match or match.groups() == ("", ""):
        return None
    size = fieldfile.size
    first, last = match.groups()
    if first:
        start, end = int(first), int(last) if last else size - 1
    else:
        start, end = max(0, size - int(last)), size - 1
    end = min(end, size - 1)
    if start > end:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{size}"
        return response

    handle = fieldfile.open("rb")
    handle.seek(start)

    def chunks(remaining=end - start + 1):
        try:
            while remaining > 0:
                data = handle.read(min(64 * 1024, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data
        finally:
            handle.close()

    response = StreamingHttpResponse(chunks(), status=206, content_type=content_type)
    response["Content-Range"] = f"bytes {start}-{end}/{size}"
    response["Content-Length"] = str(end - start + 1)
    return response


@require_GET
def asset(request, token, pk, asset_pk):
    reviewer, denied = _gate(request, token)
    if denied:
        return denied
    if not approvals.device_is_trusted(request, reviewer):
        return _unavailable(request, "request", 404)
    item = (ApprovalAsset.objects
            .filter(pk=asset_pk, request_id=pk,
                    request__recipients__reviewer=reviewer,
                    request__activity__project__client=reviewer.client,
                    request__activity__project__is_archived=False)
            .first())
    if item is None or not item.file:
        return _unavailable(request, "request", 404)

    kind = ("image" if item.is_image else "video" if item.is_video
            else "audio" if item.is_audio else "pdf" if item.is_pdf else "")
    inline = kind in INLINE and request.GET.get("download") != "1"
    content_type = mimetypes.guess_type(item.file.name)[0] or "application/octet-stream"

    try:
        response = _ranged(request, item.file, content_type) if inline else None
        if response is None:
            response = FileResponse(item.file.open("rb"), content_type=content_type,
                                    as_attachment=not inline,
                                    filename=item.display_name)
    except FileNotFoundError:
        return _unavailable(request, "request", 404)

    response["Accept-Ranges"] = "bytes"
    response["X-Content-Type-Options"] = "nosniff"
    if not inline:
        # Anything that isn't a picture, a player or a PDF is a download, and
        # never a document the browser would run: an SVG or HTML file uploaded
        # for review must not execute on this origin.
        response["Content-Security-Policy"] = "sandbox"
    _harden(response)
    response["Cache-Control"] = "private, max-age=600"
    return response
