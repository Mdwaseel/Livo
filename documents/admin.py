from django.contrib import admin
from .models import (
    Document, DocumentType, DocumentVersion, GeneratedFile, LineItem, Template,
)


@admin.register(DocumentType)
class DocumentTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "order", "is_priced", "is_active")
    list_filter = ("category", "is_active", "is_priced")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "document_type", "is_default")


class LineItemInline(admin.TabularInline):
    model = LineItem
    extra = 0


class VersionInline(admin.TabularInline):
    model = DocumentVersion
    extra = 0
    readonly_fields = ("version_number", "created_by", "created_at")


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "document_type", "project", "status", "created_at")
    list_filter = ("status", "document_type")
    search_fields = ("title", "project__name", "project__client__name")
    inlines = [LineItemInline, VersionInline]


admin.site.register(GeneratedFile)
admin.site.site_header = "Livo OS"
admin.site.site_title = "Livo OS"
admin.site.index_title = "Administration"
