from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Project


@receiver(post_save, sender=Project)
def seed_onboarding_checklist(sender, instance, created, **kwargs):
    """Every new project gets the default onboarding checklist.
    ensure_onboarding() is idempotent, so this can never duplicate items."""
    if not created:
        return
    instance.ensure_onboarding()


@receiver(post_save, sender=Project)
def add_creator_as_member(sender, instance, created, **kwargs):
    """Whoever creates a project is on it.

    Membership governs visibility now, so without this a Project Manager could
    create a project and immediately lose sight of it — the redirect straight
    after saving would 404. A signal rather than a line in the view because
    projects are also created by the clients app's default-project signal and
    by imports, and all of them want the same answer.
    """
    if created and instance.created_by_id:
        instance.members.add(instance.created_by_id)


# String sender: lazy reference avoids importing finance at module load
# (finance.models already imports projects.models).
@receiver(post_save, sender="finance.Payment")
def sync_onboarding_on_payment(sender, instance, **kwargs):
    """Recording or editing a payment may satisfy 'Advance received'."""
    instance.project.sync_onboarding()


@receiver(post_delete, sender="finance.Payment")
def sync_onboarding_on_payment_delete(sender, instance, **kwargs):
    """Deleting a payment may un-satisfy 'Advance received'."""
    instance.project.sync_onboarding()
