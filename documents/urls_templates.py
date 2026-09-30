"""URLs for the visual template designer, mounted at /templates/."""
from django.urls import path
from . import views_templates as views

app_name = "doc_templates"
urlpatterns = [
    path("", views.template_list, name="list"),
    path("new/", views.template_create, name="create"),
    path("<int:pk>/", views.template_edit, name="edit"),
    path("<int:pk>/delete/", views.template_delete, name="delete"),
]
