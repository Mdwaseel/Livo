from django.urls import path
from . import views

app_name = "clients"
urlpatterns = [
    path("", views.client_list, name="list"),
    path("new/", views.client_create, name="create"),
    path("bulk-delete/", views.client_bulk_delete, name="bulk_delete"),
    path("<int:pk>/", views.client_detail, name="detail"),
    path("<int:pk>/edit/", views.client_edit, name="edit"),
    path("<int:pk>/delete/", views.client_delete, name="delete"),
]
