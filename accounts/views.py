"""Super-admin screens: roles & the permission matrix, departments,
designations, and user org/role assignment.

Two gates, not one. The role matrix, departments and designations are
configuration SHARED by every workspace, so editing them is
@home_admin_required — a partner super admin runs their own world, not ours,
and `is_superadmin` is True for them. User assignment stays
@superadmin_required but is workspace-narrowed inside the view, so a partner can
staff their own team without ever seeing or touching ours.
"""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import log_activity
from core.tenancy import home_admin_required, is_home_admin, scope
from .models import (ACTION_LABELS, ACTIONS, Department, Designation, Module,
                     Role, RolePermission)
from .permissions import superadmin_required

User = get_user_model()


def _positive_int(raw, fallback):
    """Seniority level from a text input. `int()` on junk used to 500."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    return value if value >= 0 else fallback


# ---------- roles & permission matrix ----------

@home_admin_required
def role_list(request):
    # user_count covers BOTH ways a role is held, matching what role_delete
    # blocks on — the two used to disagree, so the list showed a role as unused
    # while deleting it was refused.
    roles = Role.objects.annotate(
        user_count=(models.Count("primary_users", distinct=True)
                    + models.Count("extra_users", distinct=True)),
        module_count=models.Count("permissions", distinct=True),
    )
    return render(request, "accounts/rbac/role_list.html", {"roles": roles})


@home_admin_required
@require_POST
def role_create(request):
    name = request.POST.get("name", "").strip()
    if not name:
        messages.error(request, "Role name is required.")
        return redirect("accounts:role_list")
    if Role.objects.filter(name__iexact=name).exists():
        messages.error(request, f"A role named “{name}” already exists.")
        return redirect("accounts:role_list")
    role = Role.objects.create(name=name,
                               description=request.POST.get("description", ""))
    log_activity(request.user, "created role", name)
    messages.success(request, f"Role “{name}” created — now set its permissions.")
    return redirect("accounts:role_matrix", pk=role.pk)


@home_admin_required
def role_matrix(request, pk):
    """The Role × Module × Action checkbox grid."""
    role = get_object_or_404(Role, pk=pk)
    modules = list(Module.objects.all())

    if request.method == "POST":
        # optional rename (non-system roles only)
        new_name = request.POST.get("name", "").strip()
        if new_name and not role.is_system and new_name != role.name:
            if Role.objects.filter(name__iexact=new_name).exclude(pk=role.pk).exists():
                messages.error(request, f"A role named “{new_name}” already exists.")
                return redirect("accounts:role_matrix", pk=role.pk)
            role.name = new_name
        role.description = request.POST.get("description", role.description)
        role.save()

        existing = {rp.module_id: rp for rp in role.permissions.all()}
        to_create, to_update, to_delete = [], [], []
        for module in modules:
            flags = {f"can_{a}": bool(request.POST.get(f"{module.key}__{a}"))
                     for a in ACTIONS}
            rp = existing.get(module.pk)
            if not any(flags.values()):
                if rp:
                    to_delete.append(rp.pk)
                continue
            if rp:
                for field, value in flags.items():
                    setattr(rp, field, value)
                to_update.append(rp)
            else:
                to_create.append(RolePermission(role=role, module=module, **flags))
        if to_delete:
            RolePermission.objects.filter(pk__in=to_delete).delete()
        if to_update:
            RolePermission.objects.bulk_update(
                to_update, [f"can_{a}" for a in ACTIONS])
        if to_create:
            RolePermission.objects.bulk_create(to_create)
        log_activity(request.user, "updated role permissions", role.name)
        messages.success(request, f"Permissions for “{role.name}” saved.")
        return redirect("accounts:role_matrix", pk=role.pk)

    perms = {rp.module_id: rp.granted_actions() for rp in role.permissions.all()}
    rows = [(m, [(a, a in perms.get(m.pk, set())) for a in ACTIONS])
            for m in modules]
    return render(request, "accounts/rbac/role_matrix.html", {
        "role": role, "rows": rows,
        "actions": [(a, ACTION_LABELS.get(a, a.capitalize())) for a in ACTIONS],
    })


@home_admin_required
@require_POST
def role_delete(request, pk):
    role = get_object_or_404(Role, pk=pk)
    if role.is_system:
        messages.error(request, f"“{role.name}” is a system role and can't be deleted.")
        return redirect("accounts:role_list")
    in_use = role.primary_users.count() + role.extra_users.count()
    if in_use:
        messages.error(request,
                       f"“{role.name}” is assigned to {in_use} user(s) — reassign "
                       "them first.")
        return redirect("accounts:role_list")
    name = role.name
    role.delete()
    log_activity(request.user, "deleted role", name)
    messages.success(request, f"Role “{name}” deleted.")
    return redirect("accounts:role_list")


# ---------- departments & designations ----------

@home_admin_required
def department_list(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if name:
            Department.objects.get_or_create(name=name, defaults={
                "description": request.POST.get("description", ""),
                "head_id": request.POST.get("head") or None,
            })
            messages.success(request, f"Department “{name}” saved.")
        return redirect("accounts:department_list")
    return render(request, "accounts/rbac/department_list.html", {
        "departments": Department.objects.select_related("head")
                                         .annotate(members_count=models.Count("members")),
        "users": User.objects.filter(is_active=True).order_by("first_name", "username"),
    })


@home_admin_required
@require_POST
def department_update(request, pk):
    dept = get_object_or_404(Department, pk=pk)
    if request.POST.get("delete"):
        name = dept.name
        dept.delete()
        messages.success(request, f"Department “{name}” deleted.")
    else:
        dept.name = request.POST.get("name", dept.name).strip() or dept.name
        dept.description = request.POST.get("description", dept.description)
        dept.head_id = request.POST.get("head") or None
        dept.save()
        messages.success(request, f"Department “{dept.name}” updated.")
    return redirect("accounts:department_list")


@home_admin_required
def designation_list(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if name:
            Designation.objects.create(
                name=name,
                department_id=request.POST.get("department") or None,
                level=_positive_int(request.POST.get("level"), 1),
            )
            messages.success(request, f"Designation “{name}” added.")
        return redirect("accounts:designation_list")
    return render(request, "accounts/rbac/designation_list.html", {
        "designations": Designation.objects.select_related("department"),
        "departments": Department.objects.all(),
    })


@home_admin_required
@require_POST
def designation_update(request, pk):
    desig = get_object_or_404(Designation, pk=pk)
    if request.POST.get("delete"):
        name = desig.name
        desig.delete()
        messages.success(request, f"Designation “{name}” deleted.")
    else:
        desig.name = request.POST.get("name", desig.name).strip() or desig.name
        desig.department_id = request.POST.get("department") or None
        desig.level = _positive_int(request.POST.get("level"), desig.level)
        desig.save()
        messages.success(request, f"Designation “{desig.name}” updated.")
    return redirect("accounts:designation_list")


# ---------- user assignment ----------

def _staffable_users(request):
    """The people this admin may re-role.

    A home admin sees everyone. A partner super admin sees only their own
    workspace's people — they need to run their team, and the employee
    directory being shared is about *seeing* colleagues, not about handing
    somebody the power to change our roles from inside their own world.
    """
    users = (User.objects.filter(is_active=True)
             .select_related("primary_role", "department", "designation",
                             "reports_to", "workspace")
             .order_by("first_name", "username"))
    if is_home_admin(request.user):
        return users
    return scope(users, request.user)


@superadmin_required
def user_list(request):
    return render(request, "accounts/rbac/user_list.html",
                  {"users": _staffable_users(request)})


@superadmin_required
def user_assign(request, pk):
    person = get_object_or_404(_staffable_users(request), pk=pk)
    if request.method == "POST":
        person.primary_role_id = request.POST.get("primary_role") or None
        person.department_id = request.POST.get("department") or None
        person.designation_id = request.POST.get("designation") or None
        reports_to = request.POST.get("reports_to") or None
        person.reports_to_id = None if reports_to == str(person.pk) else reports_to
        person.employee_code = request.POST.get("employee_code", "")
        person.save()
        person.extra_roles.set(request.POST.getlist("extra_roles"))
        log_activity(request.user, "updated user roles/org", person.username)
        messages.success(request, f"Updated {person.get_full_name() or person.username}.")
        return redirect("accounts:user_list")
    return render(request, "accounts/rbac/user_assign.html", {
        "person": person,
        "roles": Role.objects.all(),
        "extra_role_ids": set(person.extra_roles.values_list("pk", flat=True)),
        "departments": Department.objects.all(),
        "designations": Designation.objects.select_related("department"),
        "managers": _staffable_users(request).exclude(pk=person.pk),
    })
