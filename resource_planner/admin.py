from django.contrib import admin

from .models import CapacityProfile, LeaveRecord


@admin.register(LeaveRecord)
class LeaveRecordAdmin(admin.ModelAdmin):
    """Leave is normally filed at /employees/leave/ and decided at
    /employees/leave/approvals/. This is here for the cases those screens don't
    cover: bulk-entering a year of public holidays, or correcting a record
    after the fact."""
    list_display = ("user", "kind", "status", "start_date", "end_date",
                    "is_half_day", "approved_by")
    list_filter = ("status", "kind", "workspace")
    search_fields = ("user__username", "user__first_name", "user__last_name",
                     "note")
    date_hierarchy = "start_date"


@admin.register(CapacityProfile)
class CapacityProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "daily_hours", "working_days")
    search_fields = ("user__username", "user__first_name", "user__last_name")
