"""Create an EmployeeProfile for every existing User (the post_save signal
only covers users created after this app shipped)."""
from django.db import migrations


def backfill(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    EmployeeProfile = apps.get_model("employees", "EmployeeProfile")
    existing = set(EmployeeProfile.objects.values_list("user_id", flat=True))
    EmployeeProfile.objects.bulk_create([
        EmployeeProfile(user_id=pk)
        for pk in User.objects.exclude(pk__in=existing).values_list("pk", flat=True)
    ])


class Migration(migrations.Migration):

    dependencies = [
        ("employees", "0001_initial"),
        ("accounts", "0003_seed_rbac"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
