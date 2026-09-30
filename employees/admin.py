from django.contrib import admin

from .models import EmployeeDocument, EmployeeProfile, SalaryRecord, Skill


class EmployeeDocumentInline(admin.TabularInline):
    model = EmployeeDocument
    extra = 0


@admin.register(EmployeeProfile)
class EmployeeProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "status", "date_of_joining", "date_of_exit")
    list_filter = ("status",)
    search_fields = ("user__username", "user__first_name", "user__last_name",
                     "user__email")
    filter_horizontal = ("skills",)
    inlines = [EmployeeDocumentInline]


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(EmployeeDocument)
class EmployeeDocumentAdmin(admin.ModelAdmin):
    list_display = ("employee", "doc_type", "uploaded_by", "created_at")
    list_filter = ("doc_type",)


@admin.register(SalaryRecord)
class SalaryRecordAdmin(admin.ModelAdmin):
    """A back door for corrections, not the way payroll is run — the screen at
    /employees/payroll/ is, and it keeps the arithmetic consistent. The computed
    columns are editable here on purpose: fixing a wrong figure after the fact
    is exactly what somebody would come to the admin for."""
    list_display = ("employee", "month", "gross", "leave_deduction",
                    "net_payable", "status", "paid_on", "mode")
    list_filter = ("status", "mode", "month", "workspace")
    search_fields = ("employee__user__username", "employee__user__first_name",
                     "employee__user__last_name", "reference")
    date_hierarchy = "month"
