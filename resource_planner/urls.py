from django.urls import path

from . import views

app_name = "resource_planner"
urlpatterns = [
    path("", views.overview, name="overview"),
    path("employees/", views.employees, name="employees"),
    path("departments/", views.departments, name="departments"),
    path("projects/", views.projects, name="projects"),
    path("week/", views.weekly, name="weekly"),
    path("month/", views.monthly, name="monthly"),

    # Drag-and-drop target. POST + JSON, gated on tasks.assign.
    path("reassign/", views.reassign, name="reassign"),

    path("capacity/<int:user_pk>/", views.capacity_edit, name="capacity_edit"),

    path("leave/", views.leave_list, name="leave"),
    path("leave/new/", views.leave_create, name="leave_create"),
    path("leave/<int:pk>/", views.leave_update, name="leave_update"),
]
