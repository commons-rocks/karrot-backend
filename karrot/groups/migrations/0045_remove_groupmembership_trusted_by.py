# Generated by Django 3.2.5 on 2021-07-14 21:34

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('groups', '0044_auto_20210714_2017'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='groupmembership',
            name='trusted_by',
        ),
    ]
