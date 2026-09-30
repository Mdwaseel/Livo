from django.urls import path

from . import views, views_approvals

app_name = "client_calendar"
urlpatterns = [
    path("", views.index, name="index"),
    path("approvals/", views_approvals.approvals_index, name="approvals"),
    path("approvals/<int:pk>/", views_approvals.request_detail, name="approval_detail"),
    path("approvals/<int:pk>/remind/", views_approvals.request_remind,
         name="approval_remind"),
    path("approvals/<int:pk>/withdraw/", views_approvals.request_withdraw,
         name="approval_withdraw"),
    path("activities/<int:activity_pk>/approval/", views_approvals.request_create,
         name="approval_create"),
    path("reviewers/<int:pk>/access/", views_approvals.reviewer_access,
         name="reviewer_access"),
    path("<int:client_pk>/", views.planner, name="planner"),
    path("<int:client_pk>/new/", views.activity_create, name="activity_create"),
    path("<int:client_pk>/link/", views.link_action, name="link_action"),
    path("<int:client_pk>/updates/", views.update_subscribers, name="update_subscribers"),
    path("activities/<int:pk>/", views.activity_edit, name="activity_edit"),
    path("activities/<int:pk>/delete/", views.activity_delete,
         name="activity_delete"),
]
