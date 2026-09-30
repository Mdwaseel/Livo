"""The page a client opens. No login — the token in the URL is the key.

What that obliges this module to get right, all of it enforced here rather
than trusted to the template:

* Only the link's own client, only rows marked `show_to_client`, only live
  projects. The queryset is built from the link, never from a request
  parameter, so no amount of URL editing reaches another client.
* A turned-off or rotated link says so plainly, and names nobody. A 404 for a
  token that never existed and a 410 for one that did are the only difference.
* Nothing indexes it and nothing leaks it: `noindex`, `no-store`, and
  `Referrer-Policy: no-referrer`, because clients click through to the live
  Instagram post and the token must not ride along in the Referer header.
* Creative previews come through a view that re-checks the token, not a bare
  `/media/` URL — an unreleased ad must not be one guessed path away.
"""
import mimetypes

from django.http import FileResponse, Http404
from django.shortcuts import render
from django.views.decorators.http import require_GET

from projects.models import Project

from . import services
from .models import ClientActivity, ClientCalendarLink


def _harden(response):
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response["Referrer-Policy"] = "no-referrer"
    response["Cache-Control"] = "private, no-store"
    return response


def _link(token):
    return (ClientCalendarLink.objects.select_related("client")
            .filter(token=token).first())


def _is_live(link):
    return link is not None and link.is_active and not link.client.is_archived


@require_GET
def calendar(request, token):
    link = _link(token)
    if not _is_live(link):
        return _harden(render(
            request, "client_calendar/public_unavailable.html",
            {"reason": "missing" if link is None else "off"},
            status=404 if link is None else 410))

    client = link.client
    base = ClientActivity.objects.filter(
        project__client=client, project__is_archived=False, show_to_client=True)
    projects = (Project.objects
                .filter(client=client, is_archived=False,
                        client_activities__show_to_client=True)
                .distinct().order_by("name"))

    context = services.build(base, request.GET, projects=projects)
    context.update({"client": client, "link": link, "mode": "public"})

    # A teammate previewing the link is not the client opening it.
    if not request.user.is_authenticated:
        link.record_view()
    return _harden(render(request, "client_calendar/public.html", context))


@require_GET
def preview(request, token, pk):
    link = _link(token)
    if not _is_live(link):
        raise Http404
    activity = (ClientActivity.objects
                .filter(pk=pk, project__client=link.client,
                        project__is_archived=False, show_to_client=True)
                .first())
    if activity is None or not activity.preview:
        raise Http404
    try:
        handle = activity.preview.open("rb")
    except FileNotFoundError:
        raise Http404
    content_type = mimetypes.guess_type(activity.preview.name)[0] or "application/octet-stream"
    response = FileResponse(handle, content_type=content_type)
    response["X-Content-Type-Options"] = "nosniff"
    _harden(response)
    # The one exception to no-store: the same thumbnail is shown in the grid,
    # the list and the day sheet, and refetching it three times is just slow.
    response["Cache-Control"] = "private, max-age=600"
    return response
