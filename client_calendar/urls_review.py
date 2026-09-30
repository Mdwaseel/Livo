from django.urls import path

from . import views_review

app_name = "client_review"

urlpatterns = [
    path("<slug:token>/", views_review.inbox, name="inbox"),
    path("<slug:token>/code/send/", views_review.send_code, name="send_code"),
    path("<slug:token>/code/check/", views_review.check_code, name="check_code"),
    path("<slug:token>/updates/", views_review.update_prefs, name="updates"),
    path("<slug:token>/updates/unsubscribe/", views_review.unsubscribe, name="unsubscribe"),
    path("<slug:token>/<int:pk>/", views_review.detail, name="detail"),
    path("<slug:token>/<int:pk>/decide/", views_review.decide, name="decide"),
    path("<slug:token>/<int:pk>/files/<int:asset_pk>/", views_review.asset, name="asset"),
]
