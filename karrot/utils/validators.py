import re

from django.conf import settings
from rest_framework import serializers
from django.utils.translation import gettext as _


def prevent_reserved_names(value):
    if value.lower() in settings.RESERVED_NAMES:
        raise serializers.ValidationError(_('%(value)s is a reserved name') % {'value': value})


username_regex = re.compile(r'^[\w.+-]+\Z', flags=re.ASCII)


def username_validator(value):
    if not username_regex.match(value):
        raise serializers.ValidationError('username_invalid')
