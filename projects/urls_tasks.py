"""Top-level /tasks/ — the personal work queue.

Lives outside projects/urls.py because it is cross-project: it answers "what's
on my plate", not "what's in this project".
"""
from django.urls import path
from . import views

app_name = "tasks"
urlpatterns = [
    path("", views.my_tasks, name="mine"),
]
