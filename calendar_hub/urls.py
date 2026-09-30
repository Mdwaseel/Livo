from django.urls import path, register_converter

from . import views


class ISODateConverter:
    """`2026-08-14` in a path. Constrained here rather than parsed in the view
    so a malformed date is a 404 from the router instead of a traceback."""

    regex = r"\d{4}-\d{2}-\d{2}"

    def to_python(self, value):
        return value

    def to_url(self, value):
        return str(value)


register_converter(ISODateConverter, "isodate")

app_name = "calendar_hub"
urlpatterns = [
    # The four views. Month is the front door.
    path("", views.month, name="month"),
    path("week/", views.week, name="week"),
    path("day/", views.day, name="day"),
    path("agenda/", views.agenda, name="agenda"),
    path("today/", views.today, name="today"),

    # Loaded on demand when a month cell is opened.
    path("day/<isodate:on>/panel/", views.day_panel, name="day_panel"),

    # Drag-and-drop target. POST + JSON, gated on the *source's* module.
    path("move/", views.move, name="move"),

    path("events/new/", views.event_create, name="event_create"),
    path("events/<int:pk>/", views.event_detail, name="event_detail"),
    path("events/<int:pk>/edit/", views.event_edit, name="event_edit"),
    path("events/<int:pk>/delete/", views.event_delete, name="event_delete"),
    path("events/<int:pk>/skip/", views.occurrence_cancel,
         name="occurrence_cancel"),
    path("events/<int:pk>/restore/<isodate:on>/", views.occurrence_restore,
         name="occurrence_restore"),

    path("export/<str:mode>.ics", views.ics_feed, name="ics"),
]
