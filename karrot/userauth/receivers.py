from django.contrib.auth import user_login_failed, user_logged_in, get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from karrot.userauth import stats
from karrot.userauth.models import create_user_photo_warmer
from karrot.users.models import User


@receiver(user_login_failed)
def failed_login(sender, credentials, **kwargs):
    stats.login_failed(email=credentials.get('email'))


@receiver(user_logged_in)
def user_logged_in_handler(sender, **kwargs):
    stats.login_successful()


@receiver(post_save, sender=User)
def user_post_save_handler(**kwargs):
    """Sends a metric to InfluxDB when a new User object is created."""
    if kwargs.get('created'):
        stats.user_created()


@receiver(post_save, sender=get_user_model())
def warm_user_photo(sender, instance, **kwargs):
    if instance.photo:
        create_user_photo_warmer(instance).warm()
