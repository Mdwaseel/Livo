"""
Dynamic RBAC enforcement layer.

Permissions live in the database (Role × Module × Action, editable at
/settings/roles/) — NOT in code. Views gate with @require_perm("<module>",
"<action>"), code asks has_perm(user, module, action), templates use
{% user_can user "module" "action" %} (accounts.templatetags.rbac_tags) or the
legacy `can.<key>` context dict, which is now computed from the same engine.

Super admins (is_superuser, or any assigned role named "Super Admin") bypass
every check.

The old role_required()/user_can() helpers remain as deprecated shims so any
straggler call sites keep working through the transition; both resolve through
has_perm().
"""
from functools import wraps

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

SUPER_ADMIN = "Super Admin"
MANAGER = "Manager"

# Old hardcoded module keys → the (module, action) pairs that now stand in for
# them (OR semantics). Drives the deprecated shims and the `can` template dict.
LEGACY_MAP = {
    "clients":    [("clients", "create"), ("clients", "edit")],
    "projects":   [("projects", "create"), ("projects", "edit")],
    "documents":  [("documents", "create"), ("documents", "edit")],
    "approve":    [("documents", "approve"), ("documents", "archive"),
                   ("documents", "delete"), ("clients", "delete"),
                   ("projects", "delete")],
    "finance":    [("finance", "create"), ("finance", "edit")],
    "onboarding": [("projects", "approve")],
    "delivery":   [("tasks", "create"), ("tasks", "edit")],
    "reports":    [("reports", "create")],
    "settings":   [("agency_settings", "edit")],
}


def _perm_cache(user):
    """{module_key: set(actions)} across primary_role + extra_roles, built once
    per request/user instance."""
    cache = getattr(user, "_rbac_cache", None)
    if cache is None:
        from .models import RolePermission
        role_ids = set(
            user.extra_roles.values_list("pk", flat=True))
        if user.primary_role_id:
            role_ids.add(user.primary_role_id)
        cache = {}
        if role_ids:
            rows = (RolePermission.objects.filter(role_id__in=role_ids)
                    .select_related("module"))
            for rp in rows:
                cache.setdefault(rp.module.key, set()).update(rp.granted_actions())
        user._rbac_cache = cache
    return cache


def _role_names(user):
    names = getattr(user, "_rbac_role_names", None)
    if names is None:
        names = set(user.extra_roles.values_list("name", flat=True))
        if user.primary_role_id:
            names.add(user.primary_role.name)
        user._rbac_role_names = names
    return names


def is_superadmin(user):
    if not getattr(user, "is_authenticated", False):
        return False
    return user.is_superuser or SUPER_ADMIN in _role_names(user)


def is_admin_level(user):
    """Super Admin OR Manager — the two roles that run the Admin section.
    Used to gate the whole admin panel; finer items keep their own checks."""
    if not getattr(user, "is_authenticated", False):
        return False
    return is_superadmin(user) or MANAGER in _role_names(user)


def has_perm(user, module_key, action):
    """The single permission check everything routes through."""
    if not getattr(user, "is_authenticated", False):
        return False
    if is_superadmin(user):
        return True
    return action in _perm_cache(user).get(module_key, set())


def has_any_perm(user, pairs):
    return any(has_perm(user, m, a) for m, a in pairs)


def users_with_perm(module_key, action, *, workspace=None):
    """Active users who hold (module, action) — as ONE queryset.

    The inverse of has_perm, for the notification paths that need "everyone who
    can approve this". Doing it in SQL keeps those paths off the
    loop-over-every-user-and-call-has_perm pattern, which cost a query each.
    Super admins are included the same way has_perm short-circuits for them.

    `workspace` narrows the result to one partition, and every notification
    caller passes it. Without it a partner submitting an invoice for approval
    would ping our approvers with its title and client name — a leak through the
    bell rather than through a list, but the same leak. Left as None the
    function is unscoped, which is correct for a system-wide audience.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    from .models import ACTIONS, Role, RolePermission

    # The action is interpolated into a field name, so it must be a known one.
    if action not in ACTIONS:
        raise ValueError(f"Unknown RBAC action: {action!r}")

    granting = set(
        RolePermission.objects
        .filter(module__key=module_key, **{f"can_{action}": True})
        .values_list("role_id", flat=True)
    )
    granting.update(
        Role.objects.filter(name=SUPER_ADMIN).values_list("pk", flat=True))

    people = (get_user_model().objects
              .filter(Q(is_superuser=True) | Q(primary_role_id__in=granting)
                      | Q(extra_roles__in=granting),
                      is_active=True)
              .distinct())
    if workspace is not None:
        from core.tenancy import restrict_to
        people = restrict_to(people, getattr(workspace, "pk", workspace))
    return people


def require_perm(module_key, action):
    """Decorator for views. Denied users are redirected back with an error
    message (the app's established denial UX — buttons for these actions are
    already hidden by the template layer, so a denial here is either a stale
    tab or a hand-crafted request)."""
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not has_perm(request.user, module_key, action):
                messages.error(
                    request,
                    f"You don't have '{action}' permission on {module_key}.")
                return redirect(request.META.get("HTTP_REFERER") or "core:dashboard")
            return view(request, *args, **kwargs)
        return wrapper
    return decorator


def superadmin_required(view):
    """For the RBAC management screens themselves."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not is_superadmin(request.user):
            raise PermissionDenied("Super admin access required.")
        return view(request, *args, **kwargs)
    return wrapper


# ---------- deprecated legacy shims (route through has_perm) ----------

def user_can(user, module):
    """DEPRECATED: old module-write check. Use has_perm(user, module, action)."""
    if not getattr(user, "is_authenticated", False):
        return False
    return has_any_perm(user, LEGACY_MAP.get(module, []))


def role_required(module):
    """DEPRECATED: use @require_perm(module, action). Redirects with a message
    (old behavior) instead of 403."""
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not user_can(request.user, module):
                messages.error(
                    request, "You don't have permission for that action.")
                return redirect(request.META.get("HTTP_REFERER") or "core:dashboard")
            return view(request, *args, **kwargs)
        return wrapper
    return decorator
