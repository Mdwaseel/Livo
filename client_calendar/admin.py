from django.contrib import admin

from .models import (ApprovalAsset, ApprovalEvent, ApprovalRecipient, ApprovalRequest,
                     ClientActivity, ClientCalendarLink, ClientReviewer)


@admin.register(ClientActivity)
class ClientActivityAdmin(admin.ModelAdmin):
    list_display = ("title", "project", "kind", "platform", "status", "date",
                    "show_to_client")
    list_filter = ("status", "kind", "platform", "show_to_client")
    search_fields = ("title", "project__name", "project__client__name")
    date_hierarchy = "date"
    raw_id_fields = ("project", "created_by")


@admin.register(ClientCalendarLink)
class ClientCalendarLinkAdmin(admin.ModelAdmin):
    list_display = ("client", "is_active", "view_count", "last_viewed_at", "rotated_at")
    list_filter = ("is_active",)
    readonly_fields = ("token", "view_count", "last_viewed_at", "rotated_at")
    raw_id_fields = ("client", "created_by")


@admin.register(ClientReviewer)
class ClientReviewerAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "client", "is_active", "verified_at", "last_seen_at")
    list_filter = ("is_active",)
    search_fields = ("name", "email", "client__name")
    # The code hash and token are credentials; nobody edits them by hand.
    exclude = ("code_hash",)
    readonly_fields = ("token", "device_epoch", "code_sent_at", "code_expires_at",
                       "code_attempts", "verified_at", "last_seen_at")
    raw_id_fields = ("client", "contact")


class ApprovalRecipientInline(admin.TabularInline):
    model = ApprovalRecipient
    extra = 0
    raw_id_fields = ("reviewer",)
    readonly_fields = ("email_count", "last_emailed_at", "first_opened_at", "last_opened_at")


class ApprovalAssetInline(admin.TabularInline):
    model = ApprovalAsset
    extra = 0


class ApprovalEventInline(admin.TabularInline):
    model = ApprovalEvent
    extra = 0
    can_delete = False
    readonly_fields = ("kind", "reviewer", "actor", "note", "ip_address", "created_at")


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ("activity", "version", "status", "respond_by", "decided_by", "decided_at")
    list_filter = ("status",)
    search_fields = ("activity__title", "activity__project__client__name")
    raw_id_fields = ("activity", "decided_by", "created_by")
    inlines = [ApprovalRecipientInline, ApprovalAssetInline, ApprovalEventInline]
