# Generated by Django 3.2.7 on 2021-11-23 16:50

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('activities', '0037_alter_activityparticipant_participant_role'),
    ]

    operations = [
        migrations.AddField(
            model_name='participantrole',
            name='description',
            field=models.TextField(blank=True),
        ),
    ]
