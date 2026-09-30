from django.contrib import admin
from .models import Client, Contact


class ContactInline(admin.TabularInline):
    model = Contact
    extra = 1


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "email", "phone", "is_archived")
    search_fields = ("name", "email", "gstin")
    list_filter = ("is_archived", "state")
    inlines = [ContactInline]
