from django.urls import path

from . import views_public

app_name = "client_calendar_public"
urlpatterns = [
    # `slug` matches exactly the alphabet `secrets.token_urlsafe` produces, so
    # anything else is a 404 from the router before a query runs.
    path("<slug:token>/", views_public.calendar, name="calendar"),
    path("<slug:token>/preview/<int:pk>/", views_public.preview, name="preview"),
]
