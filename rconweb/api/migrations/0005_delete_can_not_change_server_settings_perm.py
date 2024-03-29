# Generated by Django 3.2.18 on 2023-04-11 03:15

from django.db import migrations


def delete_old_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes.ContentType")
    Permission = apps.get_model("auth.Permission")
    RconUser = apps.get_model("api.RconUser")

    content_type = ContentType.objects.get_for_model(RconUser)
    try:
        Permission.objects.filter(
            content_type=content_type, codename="can_not_change_server_settings"
        ).delete()
    except Permission.DoesNotExist:
        pass


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0004_delete_default_perms"),
    ]

    operations = [migrations.RunPython(delete_old_permission)]
