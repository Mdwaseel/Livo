from django.urls import path, register_converter

from . import views


class DatasetConverter:
    """Only the four exportable datasets reach the view. Constraining it here
    means an unknown key is a 404 from the router rather than a branch inside
    the export handler."""
    regex = "employees|projects|clients|departments"

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


class FormatConverter:
    regex = "csv|xlsx"

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


register_converter(DatasetConverter, "dataset")
register_converter(FormatConverter, "exportfmt")

app_name = "analytics"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("employees/", views.employees, name="employees"),
    path("projects/", views.projects, name="projects"),
    path("finance/", views.finance, name="finance"),
    path("clients/", views.clients, name="clients"),
    path("export/<dataset:dataset>.<exportfmt:fmt>", views.export, name="export"),
]
