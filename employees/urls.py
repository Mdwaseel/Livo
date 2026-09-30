from django.urls import path

from . import views, views_leave, views_salary

app_name = "employees"
urlpatterns = [
    path("", views.directory, name="directory"),
    path("new/", views.create, name="create"),
    path("me/", views.my_profile, name="me"),

    # --- leave (self-service + approvals) ---
    path("leave/", views_leave.my_leave, name="my_leave"),
    path("leave/request/", views_leave.leave_request, name="leave_request"),
    path("leave/<int:pk>/cancel/", views_leave.leave_cancel, name="leave_cancel"),
    path("leave/approvals/", views_leave.approvals, name="leave_approvals"),
    path("leave/<int:pk>/decide/", views_leave.leave_decide, name="leave_decide"),

    # --- salary ---
    path("salary/", views_salary.my_salary, name="my_salary"),
    path("payroll/", views_salary.payroll_month, name="payroll"),
    path("payroll/generate/", views_salary.payroll_generate,
         name="payroll_generate"),
    path("payroll/<int:pk>/pay/", views_salary.salary_pay, name="salary_pay"),
    path("payroll/<int:pk>/adjust/", views_salary.salary_adjust,
         name="salary_adjust"),

    # --- profiles (kept last: <int:pk> would swallow the paths above) ---
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/edit/", views.edit_basic, name="edit_basic"),
    path("<int:pk>/hr/", views.edit_hr, name="edit_hr"),
    path("<int:pk>/offboard/", views.offboard, name="offboard"),
    path("<int:pk>/reactivate/", views.reactivate, name="reactivate"),
    path("<int:pk>/delete/", views.delete, name="delete"),
    path("<int:pk>/documents/upload/", views.document_upload, name="document_upload"),
    path("documents/<int:doc_pk>/download/", views.document_download,
         name="document_download"),
    path("documents/<int:doc_pk>/delete/", views.document_delete,
         name="document_delete"),
]
