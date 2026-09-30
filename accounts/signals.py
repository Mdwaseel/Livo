"""Transition shim: while the deprecated User.role CharField still exists,
newly created users automatically get the matching RBAC primary_role, so code
paths (and tests) that only set the old field keep working."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Role, User
from .rbac_seed import LEGACY_USER_ROLE_MAP


@receiver(post_save, sender=User)
def assign_primary_role_from_legacy(sender, instance, created, **kwargs):
    if not created or instance.primary_role_id:
        return
    # Blank is the field's default, so this only fires when a caller named a
    # legacy role explicitly — never for users created without one.
    role_name = LEGACY_USER_ROLE_MAP.get(instance.role or "")
    if not role_name:
        return
    role = Role.objects.filter(name=role_name).first()
    if role:
        instance.primary_role = role
        instance.save(update_fields=["primary_role"])
