from django.apps import AppConfig


class CalendarHubConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "calendar_hub"
    verbose_name = "Calendar"

    def ready(self):
        # Importing the package registers every source adapter. Without this the
        # registry is empty until something happens to import a source module,
        # and the calendar would render blank in a way that looks like missing
        # data rather than missing wiring.
        from . import sources  # noqa: F401
