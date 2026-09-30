from django.urls import path
from . import views

app_name = "projects"
urlpatterns = [
    path("", views.project_list, name="list"),
    path("new/", views.project_create, name="create_standalone"),
    path("new/<int:client_pk>/", views.project_create, name="create"),
    path("bulk-delete/", views.project_bulk_delete, name="bulk_delete"),
    path("<int:pk>/", views.project_detail, name="detail"),
    path("<int:pk>/edit/", views.project_edit, name="edit"),
    path("<int:pk>/members/", views.project_members, name="members"),
    path("<int:pk>/delete/", views.project_delete, name="delete"),
    # tasks
    path("<int:pk>/tasks/new/", views.task_create, name="task_create"),
    path("tasks/<int:task_pk>/", views.task_detail, name="task_detail"),
    path("tasks/<int:task_pk>/status/", views.task_set_status, name="task_set_status"),
    path("tasks/<int:task_pk>/edit/", views.task_edit, name="task_edit"),
    path("tasks/<int:task_pk>/assign/", views.task_assign, name="task_assign"),
    path("tasks/<int:task_pk>/delete/", views.task_delete, name="task_delete"),
    # task review workflow
    path("tasks/<int:task_pk>/submit/", views.task_submit, name="task_submit"),
    path("tasks/<int:task_pk>/approve/", views.task_approve, name="task_approve"),
    path("tasks/<int:task_pk>/reopen/", views.task_reopen, name="task_reopen"),
    # task dependencies
    path("tasks/<int:task_pk>/dependencies/", views.task_deps_update,
         name="task_deps_update"),
    # task checklist
    path("tasks/<int:task_pk>/checklist/add/", views.checklist_add,
         name="checklist_add"),
    path("checklist/<int:item_pk>/toggle/", views.checklist_toggle,
         name="checklist_toggle"),
    path("checklist/<int:item_pk>/move/", views.checklist_move,
         name="checklist_move"),
    path("checklist/<int:item_pk>/delete/", views.checklist_delete,
         name="checklist_delete"),
    # task comments
    path("tasks/<int:task_pk>/comments/add/", views.comment_add, name="comment_add"),
    path("comments/<int:comment_pk>/delete/", views.comment_delete,
         name="comment_delete"),
    # task attachments
    path("tasks/<int:task_pk>/attachments/add/", views.attachment_upload,
         name="attachment_upload"),
    path("attachments/<int:attachment_pk>/delete/", views.attachment_delete,
         name="attachment_delete"),
    # work log
    path("<int:pk>/worklog/new/", views.worklog_create, name="worklog_create"),
    path("worklog/<int:entry_pk>/edit/", views.worklog_edit, name="worklog_edit"),
    path("worklog/<int:entry_pk>/delete/", views.worklog_delete, name="worklog_delete"),
    path("worklog/<int:entry_pk>/submit/", views.worklog_submit,
         name="worklog_submit"),
    path("worklog/<int:entry_pk>/approve/", views.worklog_approve,
         name="worklog_approve"),
    path("worklog/<int:entry_pk>/reject/", views.worklog_reject,
         name="worklog_reject"),
    # assets
    path("<int:pk>/assets/new/", views.asset_upload, name="asset_upload"),
    path("assets/<int:asset_pk>/delete/", views.asset_delete, name="asset_delete"),
    # milestones
    path("<int:pk>/milestones/new/", views.milestone_create, name="milestone_create"),
    path("milestones/<int:milestone_pk>/edit/", views.milestone_edit,
         name="milestone_edit"),
    path("milestones/<int:milestone_pk>/status/", views.milestone_set_status,
         name="milestone_set_status"),
    path("milestones/<int:milestone_pk>/delete/", views.milestone_delete,
         name="milestone_delete"),
    # onboarding
    path("onboarding/<int:item_pk>/toggle/", views.onboarding_toggle,
         name="onboarding_toggle"),
]
