from django.urls import path

from . import views, views_auth

app_name = "accounts"
urlpatterns = [
    # --- sign-in (password, then an emailed one-time code) ---
    path("login/", views_auth.login_view, name="login"),
    path("login/verify/", views_auth.login_otp_view, name="login_otp"),
    path("login/verify/resend/", views_auth.login_otp_resend, name="login_otp_resend"),
    path("logout/", views_auth.logout_view, name="logout"),

    # --- forgotten password ---
    path("password/forgot/", views_auth.PasswordResetRequestView.as_view(),
         name="password_reset"),
    path("password/forgot/sent/", views_auth.PasswordResetSentView.as_view(),
         name="password_reset_done"),
    path("password/reset/<uidb64>/<token>/",
         views_auth.PasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("password/reset/done/", views_auth.PasswordResetFinishedView.as_view(),
         name="password_reset_complete"),

    # --- RBAC management (super admin only) ---
    path("roles/", views.role_list, name="role_list"),
    path("roles/new/", views.role_create, name="role_create"),
    path("roles/<int:pk>/", views.role_matrix, name="role_matrix"),
    path("roles/<int:pk>/delete/", views.role_delete, name="role_delete"),
    path("departments/", views.department_list, name="department_list"),
    path("departments/<int:pk>/", views.department_update, name="department_update"),
    path("designations/", views.designation_list, name="designation_list"),
    path("designations/<int:pk>/", views.designation_update, name="designation_update"),
    path("team/", views.user_list, name="user_list"),
    path("team/<int:pk>/", views.user_assign, name="user_assign"),
]
