from django.urls import path
from . import views

app_name = "documents"
urlpatterns = [
    path("", views.document_list, name="list"),
    path("new/<int:project_pk>/<slug:type_slug>/", views.document_create, name="create"),
    path("monthly-report/<int:project_pk>/", views.monthly_report_create,
         name="monthly_report"),
    path("<int:pk>/", views.document_detail, name="detail"),
    path("<int:pk>/save/", views.document_save, name="save"),
    path("<int:pk>/autosave/", views.document_autosave, name="autosave"),
    path("<int:pk>/items/", views.document_items_save, name="save_items"),
    path("<int:pk>/status/", views.document_set_status, name="set_status"),
    path("<int:pk>/export-pdf/", views.document_export_pdf, name="export_pdf"),
    path("<int:pk>/regenerate/", views.document_regenerate, name="regenerate"),
    path("<int:pk>/duplicate/", views.document_duplicate, name="duplicate"),
    path("bulk/", views.document_bulk_action, name="bulk_action"),
    path("<int:pk>/delete/", views.document_delete, name="delete"),
    path("<int:pk>/archive/", views.document_archive, name="archive"),
    path("<int:pk>/unarchive/", views.document_unarchive, name="unarchive"),
    path("<int:pk>/compare/", views.document_compare, name="compare"),
    path("<int:pk>/restore/<int:version_number>/", views.document_restore_version,
         name="restore_version"),
]
