"""Top-level /worklogs/ — the personal timesheet and the approvals queue.

Cross-project, like /tasks/: these answer "what did I log this week" and "what
is waiting on me", not "what happened on this project".
"""
from django.urls import path
from . import views

app_name = "worklogs"
urlpatterns = [
    path("", views.my_worklogs, name="mine"),
    # Project comes from a POST field here, unlike the project-scoped route.
    path("new/", views.worklog_create, name="create"),
    path("submit-week/", views.worklog_submit_week, name="submit_week"),
    path("approvals/", views.worklog_approvals, name="approvals"),
]
