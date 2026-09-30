from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Client


@receiver(post_save, sender=Client)
def create_default_project(sender, instance, created, **kwargs):
    """Every new client gets a default project so simple clients feel
    like Client -> Documents, while multi-engagement clients still get structure."""
    if not created:
        return
    from projects.models import Project  # lazy import avoids circular import
    if not instance.projects.exists():
        Project.objects.create(
            client=instance,
            name=f"{instance.name} - General",
            created_by=instance.created_by,
            is_default=True,
            # Inherited from the client, not from whoever happens to be logged
            # in. A project auto-created here with no workspace would read as
            # ours (NULL means home) — so a partner's very first project would
            # land in our lists and be invisible in theirs.
            workspace_id=instance.workspace_id,
        )
