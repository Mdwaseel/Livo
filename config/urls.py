from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import include, path

# /admin/login/ is a second front door: it takes a username and password and
# signs staff straight in, which would step around the emailed one-time code
# entirely. Wrapping it sends anyone who isn't already signed in through
# accounts:login instead; Django's own view then forwards authenticated staff
# on to the admin index. Must happen before admin.site.urls is evaluated below,
# because that property builds the URL table from the current attributes.
admin.site.login = login_required(admin.site.login)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("templates/", include("documents.urls_templates")),
    path("", include("core.urls")),
    path("accounts/", include("accounts.urls")),
    path("clients/", include("clients.urls")),
    path("projects/", include("projects.urls")),
    path("tasks/", include("projects.urls_tasks")),
    path("worklogs/", include("projects.urls_worklogs")),
    path("documents/", include("documents.urls")),
    path("finance/", include("finance.urls")),
    path("employees/", include("employees.urls")),
    path("analytics/", include("analytics.urls")),
    path("planning/", include("resource_planner.urls")),
    path("calendar/", include("calendar_hub.urls")),
    path("client-calendars/", include("client_calendar.urls")),
    # The link a client opens. Short, because it gets pasted into WhatsApp,
    # and outside every app prefix, because nothing about it is signed in.
    path("c/", include("client_calendar.urls_public")),
    # Where an approval email lands. One reviewer's token, gated by a code
    # emailed to that reviewer — see client_calendar/views_review.py.
    path("r/", include("client_calendar.urls_review")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.BASE_DIR / "static")
