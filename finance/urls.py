from django.urls import path
from . import views

app_name = "finance"
urlpatterns = [
    path("projects/<int:project_pk>/payments/new/", views.payment_create, name="payment_create"),
    path("payments/<int:pk>/edit/", views.payment_edit, name="payment_edit"),
    path("payments/<int:pk>/delete/", views.payment_delete, name="payment_delete"),
]
