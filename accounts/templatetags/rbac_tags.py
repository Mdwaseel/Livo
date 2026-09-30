from django import template

from accounts.permissions import has_perm, is_superadmin

register = template.Library()


@register.simple_tag
def user_can(user, module_key, action):
    """{% user_can user 'projects' 'create' as ok %} or inline in {% if %}:
    {% user_can user 'projects' 'create' as ok %}{% if ok %}…{% endif %}"""
    return has_perm(user, module_key, action)


@register.filter
def superadmin(user):
    """{% if user|superadmin %} — for RBAC management links."""
    return is_superadmin(user)
