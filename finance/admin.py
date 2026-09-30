from django.contrib import admin

from .models import Payment


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("project", "amount", "payment_type", "method",
                    "reference", "received_on", "recorded_by")
    list_filter = ("payment_type", "method", "received_on")
    search_fields = ("project__name", "project__client__name", "reference")
    date_hierarchy = "received_on"
    autocomplete_fields = ()
    raw_id_fields = ("project", "invoice")
