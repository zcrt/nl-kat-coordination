# Generated by Django 3.2.5 on 2021-10-27 12:48

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tools", "0008_organizationmember_verified")]

    operations = [
        migrations.AddField(model_name="scanprofile", name="is_source_ooi", field=models.BooleanField(default=False))
    ]
