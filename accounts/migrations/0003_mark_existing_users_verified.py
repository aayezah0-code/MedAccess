from django.db import migrations


def mark_existing_users_verified(apps, schema_editor):
    """
    Marks all existing registered users as email_verified=True so that
    existing patients, pharmacies, and admin accounts retain full access.
    """
    User = apps.get_model('accounts', 'User')
    User.objects.all().update(email_verified=True)


def reverse_mark_existing_users_verified(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_user_email_verified'),
    ]

    operations = [
        migrations.RunPython(
            mark_existing_users_verified,
            reverse_code=reverse_mark_existing_users_verified,
        ),
    ]

