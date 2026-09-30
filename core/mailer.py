"""The two things every outbound notification email in this project needs.

`projects/emails.py` grew both of these while it was the only module sending
mail. It is no longer — a project membership and a meeting invitation are the
other two events that reach someone who isn't looking at the app — so they live
here rather than being copied, or worse, being copied slightly differently.

The rule that shapes `send` is the one that shaped the assignment email: **the
delivery must never break the thing it is announcing.** By the time it runs, the
membership is granted or the meeting is in the calendar and visible. A mail
server that is slow, misconfigured or down must not turn that successful save
into a 500 for the person who made it, so every send is wrapped and logged and
the failure is the operator's problem, not the user's.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def absolute_url(path, request=None):
    """Turn an app path into a full URL for an email link.

    A request knows its own host. A cron job does not, so it falls back to the
    SITE_URL setting. With neither available the link is dropped — an email with
    no button is better than one whose button 404s.
    """
    if not path:
        return ""
    if request is not None:
        return request.build_absolute_uri(path)
    base = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    return f"{base}{path}" if base else ""


def deliverable(user):
    """Can this person actually be emailed? Active, and with an address on file."""
    return bool(user is not None and getattr(user, "is_active", False)
                and getattr(user, "email", ""))


def send(*, subject, template, context, to, attachments=(), failure_note="",
         reply_to=(), headers=None):
    """Render `template`.txt / `.html` and send them as one message.

    `template` is the stem — "calendar_hub/email/event_invite" picks up both
    halves. The plain-text part is the body rather than an afterthought: it is
    what a text-only client, a screen reader in plain-text mode and every spam
    filter actually read.

    Returns True only when the message reached the mail backend, so callers and
    tests can tell "sent" from "deliberately skipped".
    """
    recipients = [address for address in to if address]
    if not recipients:
        return False

    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=render_to_string(f"{template}.txt", context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=recipients,
            # The From address is a no-reply mailbox; a client who answers an
            # approval email should reach the person who asked.
            reply_to=[address for address in reply_to if address] or None,
            headers=headers or None,
        )
        message.attach_alternative(
            render_to_string(f"{template}.html", context), "text/html")
        for name, content, mimetype in attachments:
            message.attach(name, content, mimetype)
        message.send(fail_silently=False)
    except Exception:
        # Deliberately broad. See the module docstring: the event being
        # announced has already happened and is visible in the app, so no mail
        # failure is worth surfacing to the user who caused it. It goes to the
        # log for whoever runs the server.
        logger.exception("Could not send %s email%s", template,
                         f" — {failure_note}" if failure_note else "")
        return False

    logger.info("Sent %s email to %s", template, ", ".join(recipients))
    return True
