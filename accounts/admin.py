from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Department, Designation, Module, Role, RolePermission, User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("Agency (deprecated)", {"fields": ("role", "phone")}),
        ("Organisation", {"fields": ("department", "designation", "reports_to",
                                     "employee_code")}),
        ("RBAC", {"fields": ("primary_role", "extra_roles")}),
    )
    list_display = ("username", "email", "primary_role", "department",
                    "designation", "is_staff")
    list_filter = ("primary_role", "department", "is_staff")
    filter_horizontal = UserAdmin.filter_horizontal + ("extra_roles",)


class RolePermissionInline(admin.TabularInline):
    model = RolePermission
    extra = 0


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("name", "is_system", "priority")
    inlines = [RolePermissionInline]


@admin.register(Module)
class ModuleAdmin(admin.ModelAdmin):
    list_display = ("key", "label", "order")
    ordering = ("order",)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "head")


@admin.register(Designation)
class DesignationAdmin(admin.ModelAdmin):
    list_display = ("name", "department", "level", "order")
