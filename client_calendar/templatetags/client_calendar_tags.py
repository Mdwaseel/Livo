from django import template
from django.utils import timezone
from django.utils.timesince import timesince

from ..icons import svg

register = template.Library()


@register.filter
def cc_ago(moment):
    """ "just now" under a minute, "3 hours ago" after.

    Django's `timesince` says "0 minutes" for anything fresh, and "last change
    0 minutes ago" on a client-facing page reads like something is broken.
    """
    if not moment:
        return ""
    if (timezone.now() - moment).total_seconds() < 60:
        return "just now"
    return f"{timesince(moment)} ago"


@register.simple_tag
def cc_icon(name, css_class="cc-ico"):
    """{% cc_icon "instagram" %} — see client_calendar/icons.py."""
    return svg(name, css_class)
