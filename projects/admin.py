from django.contrib import admin
from .models import (
    Asset, Milestone, OnboardingItem, Project, RecurringTask, Sprint, Task,
    TaskAttachment, TaskChecklistItem, TaskComment, WorkLogEntry,
)


class MilestoneInline(admin.TabularInline):
    model = Milestone
    extra = 1


class OnboardingItemInline(admin.TabularInline):
    model = OnboardingItem
    extra = 0
    fields = ("order", "key", "label", "is_done", "is_auto",
              "completed_on", "completed_by")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "client", "status", "project_type", "is_default")
    list_filter = ("status", "is_default")
    search_fields = ("name", "client__name")
    inlines = [MilestoneInline, OnboardingItemInline]


class TaskChecklistItemInline(admin.TabularInline):
    model = TaskChecklistItem
    extra = 0
    fields = ("order", "text", "is_done")


class TaskCommentInline(admin.TabularInline):
    model = TaskComment
    extra = 0
    fields = ("author", "body", "created_at")
    readonly_fields = ("created_at",)


class TaskAttachmentInline(admin.TabularInline):
    model = TaskAttachment
    extra = 0
    fields = ("file", "uploaded_by", "created_at")
    readonly_fields = ("created_at",)


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "project", "status", "approval_status", "priority",
                    "assignee", "reviewer", "due_date", "completed_on")
    list_filter = ("status", "approval_status", "priority", "is_billable",
                   "department", "sprint")
    search_fields = ("title", "project__name", "project__client__name")
    autocomplete_fields = ("project",)
    filter_horizontal = ("blocked_by",)
    inlines = [TaskChecklistItemInline, TaskCommentInline, TaskAttachmentInline]
    fieldsets = (
        (None, {"fields": ("project", "title", "description", "sprint")}),
        ("Assignment", {"fields": ("status", "priority", "assignee", "department",
                                   "due_date", "completed_on", "order", "created_by")}),
        ("Effort", {"fields": ("estimated_hours", "actual_hours", "is_billable")}),
        ("Review", {"fields": ("reviewer", "approval_status", "approved_by",
                               "approved_on")}),
        ("Dependencies", {"fields": ("blocked_by",)}),
    )


@admin.register(Sprint)
class SprintAdmin(admin.ModelAdmin):
    list_display = ("name", "project", "start_date", "end_date", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "project__name")


@admin.register(TaskChecklistItem)
class TaskChecklistItemAdmin(admin.ModelAdmin):
    list_display = ("text", "task", "is_done", "order")
    list_filter = ("is_done",)
    search_fields = ("text", "task__title")


@admin.register(TaskComment)
class TaskCommentAdmin(admin.ModelAdmin):
    list_display = ("task", "author", "created_at")
    search_fields = ("body", "task__title")
    date_hierarchy = "created_at"


@admin.register(TaskAttachment)
class TaskAttachmentAdmin(admin.ModelAdmin):
    list_display = ("filename", "task", "uploaded_by", "created_at")
    search_fields = ("file", "task__title")


@admin.register(RecurringTask)
class RecurringTaskAdmin(admin.ModelAdmin):
    list_display = ("title", "project", "frequency", "next_run", "assignee",
                    "reviewer", "is_active")
    list_filter = ("frequency", "is_active", "department")
    search_fields = ("title", "project__name")


@admin.register(WorkLogEntry)
class WorkLogEntryAdmin(admin.ModelAdmin):
    list_display = ("date", "project", "task", "category", "hours", "is_billable",
                    "approval_status", "logged_by")
    list_filter = ("category", "approval_status", "is_billable", "date")
    search_fields = ("description", "project__name", "project__client__name",
                     "task__title")
    date_hierarchy = "date"
    autocomplete_fields = ("project", "task")
    fieldsets = (
        (None, {"fields": ("project", "task", "date", "category", "description")}),
        ("Time", {"fields": ("start_time", "end_time", "hours", "is_billable"),
                  "description": "Leave hours blank to derive it from start/end."}),
        ("Evidence", {"fields": ("screenshot", "logged_by")}),
        ("Approval", {"fields": ("approval_status", "approved_by", "approved_on",
                                 "review_note")}),
    )


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("title", "project", "category", "is_report_proof", "uploaded_by")
    list_filter = ("category", "is_report_proof")
    search_fields = ("title", "project__name", "project__client__name")
