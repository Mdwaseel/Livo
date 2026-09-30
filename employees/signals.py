"""Every User gets an EmployeeProfile automatically, so views can rely on
request.user.employee_profile existing (mirrors the clients/projects signal
pattern)."""
from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import EmployeeProfile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_employee_profile(sender, instance, created, **kwargs):
    if created:
        EmployeeProfile.objects.get_or_create(user=instance)
