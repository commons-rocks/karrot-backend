import json

import requests
from django.conf import settings
from django.db.models.signals import post_save, pre_delete, post_delete
from django.dispatch import receiver

from karrot.conversations.models import Conversation
from karrot.groups import roles, stats
from karrot.groups.emails import prepare_user_became_editor_email, prepare_user_lost_editor_role_email, \
    prepare_user_got_role_email
from karrot.groups.models import Group, GroupMembership, Trust, create_group_photo_warmer
from karrot.groups.roles import GROUP_EDITOR
from karrot.history.models import History, HistoryTypus
from karrot.utils import frontend_urls


@receiver(post_save, sender=Group)
def group_created(sender, instance, created, **kwargs):
    """Ensure every group has a conversation."""
    if not created:
        return
    group = instance
    conversation = Conversation.objects.get_or_create_for_target(group)
    conversation.sync_users(group.members.all())


@receiver(pre_delete, sender=Group)
def group_deleted(sender, instance, **kwargs):
    """Delete the conversation when the group is deleted."""
    group = instance
    conversation = Conversation.objects.get_for_target(group)
    if conversation:
        conversation.delete()


@receiver(post_save, sender=GroupMembership)
def group_member_added(sender, instance, created, **kwargs):
    if not created:
        return
    group = instance.group
    user = instance.user

    conversation = Conversation.objects.get_or_create_for_target(group)
    conversation.join(user, muted=False)

    stats.group_joined(group)


@receiver(pre_delete, sender=GroupMembership)
def group_member_removed(sender, instance, **kwargs):
    group = instance.group
    user = instance.user

    # leave all conversations related to this group
    for conversation in Conversation.objects.filter(group=group, participants__in=[user]):
        conversation.leave(user)

    stats.group_left(group)


@receiver(post_save, sender=Trust)
def trust_given(sender, instance, created, **kwargs):
    if not created:
        return

    trust = instance

    if trust.role == GROUP_EDITOR:
        membership = trust.membership
        relevant_trust_count = Trust.objects.filter(membership=membership, role=GROUP_EDITOR).count()
        trust_threshold = membership.group.trust_threshold_for_newcomer()

        if relevant_trust_count >= trust_threshold and roles.GROUP_EDITOR not in membership.roles:
            membership.add_roles([roles.GROUP_EDITOR])
            membership.save()

            History.objects.create(
                typus=HistoryTypus.MEMBER_BECAME_EDITOR,
                group=membership.group,
                users=[membership.user],
                payload={
                    'threshold': trust_threshold,
                },
            )

            prepare_user_became_editor_email(user=membership.user, group=membership.group).send()

            stats.member_became_editor(membership.group)

    else:
        # trust for some other role
        membership = trust.membership
        relevant_trust_count = Trust.objects.filter(membership=membership, role=trust.role).count()
        role = membership.group.roles.get(name=trust.role)

        if relevant_trust_count >= role.threshold:
            membership.add_roles([trust.role])
            membership.save()

            History.objects.create(
                typus=HistoryTypus.MEMBER_GOT_ROLE,
                group=membership.group,
                users=[membership.user],
                payload={
                    'role': {
                        'name': role.name,
                        'display_name': role.display_name,
                    },
                }
            )

            role = membership.group.roles.get(name=trust.role)

            prepare_user_got_role_email(user=membership.user, group=membership.group, role=role).send()

            # TODO: stats

    stats.trust_given(trust)


@receiver(post_delete, sender=Trust)
def trust_revoked(sender, instance, **kwargs):
    trust = instance

    if trust.role == GROUP_EDITOR:
        membership = trust.membership
        relevant_trust = Trust.objects.filter(membership=membership, role=GROUP_EDITOR)
        trust_threshold = membership.group.trust_threshold_for_newcomer()

        if relevant_trust.count() < trust_threshold and roles.GROUP_EDITOR in membership.roles:
            membership.remove_roles([roles.GROUP_EDITOR])
            membership.save()

            History.objects.create(
                typus=HistoryTypus.USER_LOST_EDITOR_ROLE,
                group=membership.group,
                users=[membership.user],
                payload={
                    'threshold': trust_threshold,
                },
            )

            prepare_user_lost_editor_role_email(user=membership.user, group=membership.group).send()

            stats.user_lost_editor_role(membership.group)

    stats.trust_revoked(trust)


@receiver(pre_delete, sender=GroupMembership)
def remove_trust(sender, instance, **kwargs):
    membership = instance

    Trust.objects.filter(given_by=membership.user, membership__group=membership.group).delete()


@receiver(post_save, sender=Group)
def notify_chat_on_group_creation(sender, instance, created, **kwargs):
    """Send notifications to admin chat"""
    if not created:
        return
    group = instance
    webhook_url = getattr(settings, 'ADMIN_CHAT_WEBHOOK', None)

    if webhook_url is None:
        return

    group_url = frontend_urls.group_preview_url(group)

    message_data = {
        'text': f':tada: A new group has been created on **{settings.SITE_NAME}**! [Visit {group.name}]({group_url})',
    }

    response = requests.post(webhook_url, data=json.dumps(message_data), headers={'Content-Type': 'application/json'})
    response.raise_for_status()


@receiver(post_save, sender=Group)
def warm_group_photo(sender, instance, **kwargs):
    if instance.photo:
        create_group_photo_warmer(instance).warm()
