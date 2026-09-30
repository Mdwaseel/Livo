"""What people see, and what the log records, when a form fails its CSRF check.

Django's default is a bare "403 Forbidden — CSRF verification failed", which
reads like a broken site and tells whoever runs the server nothing about why.
The usual real-world causes are mundane — a page left open across a sign-in, a
browser or extension that strips the Referer, cookies blocked in an in-app
browser — so the page says "this expired, go back and try again", and the log
line names Django's reason and what the request was missing, so a pattern can
be spotted and fixed rather than guessed at.
"""
import logging

from django.shortcuts import render

logger = logging.getLogger("django.security.csrf")


def csrf_failure(request, reason=""):
    logger.warning(
        "CSRF failure: %s | %s %s | origin=%s referer=%s csrf_cookie=%s ua=%s",
        reason, request.method, request.path,
        request.META.get("HTTP_ORIGIN", "-"),
        request.META.get("HTTP_REFERER", "-"),
        "yes" if "csrftoken" in request.COOKIES else "no",
        request.META.get("HTTP_USER_AGENT", "-")[:160])
    response = render(request, "403_csrf.html", status=403)
    response["Cache-Control"] = "no-store"
    return response
