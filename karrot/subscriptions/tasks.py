from itertools import groupby

from babel.dates import format_date, format_time
from django.utils import timezone, translation
from django.utils.text import Truncator
from huey import crontab
from huey.contrib.djhuey import db_task, db_periodic_task

from karrot.applications.models import ApplicationStatus
from karrot.groups.models import GroupMembership, GroupNotificationType
from karrot.subscriptions.models import ChannelSubscription, WebPushSubscription
from karrot.subscriptions.web_push import notify_subscribers
from karrot.utils import frontend_urls, stats_utils
from karrot.utils.stats_utils import timer


@db_task()
def notify_message_push_subscribers(message):
    if message.is_thread_reply():
        subscriptions = WebPushSubscription.objects.filter(
            user__conversationthreadparticipant__thread=message.thread,
            user__conversationthreadparticipant__muted=False,
        )
    else:
        subscriptions = WebPushSubscription.objects.filter(
            user__conversationparticipant__conversation=message.conversation,
            user__conversationparticipant__muted=False,
        )

    subscriptions = subscriptions.\
        exclude(user=message.author).\
        select_related('user').\
        order_by('user__language').\
        distinct()

    for (language, subscriptions) in groupby(subscriptions, key=lambda subscription: subscription.user.language):
        subscriptions = list(subscriptions)
        notify_message_push_subscribers_with_language(message, subscriptions, language)


def get_message_title(message, language):
    conversation = message.conversation
    author_name = message.author.display_name
    type = conversation.type()

    if message.is_thread_reply():
        thread_start = Truncator(message.thread.content).chars(num=15)
        return '{} / {}'.format(thread_start, author_name)

    if type == 'group':
        return '{} / {}'.format(conversation.target.name, author_name)

    if type == 'place':
        return '{} / {}'.format(conversation.target.name, author_name)

    if type == 'activity':
        activity = conversation.target
        group_tz = activity.place.group.timezone
        with timezone.override(group_tz):
            weekday = format_date(
                activity.date.start.astimezone(timezone.get_current_timezone()),
                'EEEE',
                locale=translation.to_locale(language),
            )
            time = format_time(
                activity.date.start,
                format='short',
                locale=translation.to_locale(language),
                tzinfo=timezone.get_current_timezone(),
            )
        short_date = '{} {}'.format(weekday, time)
        short_name = '{} {}'.format(activity.activity_type.get_translated_name(), short_date)
        return '{} / {}'.format(short_name, author_name)

    if type == 'application':
        application = conversation.target
        applicant_name = application.user.display_name
        if applicant_name == '':
            applicant_name = '(?)'

        emoji = '❓'
        if application.status == ApplicationStatus.ACCEPTED.value:
            emoji = '✅'
        elif application.status == ApplicationStatus.DECLINED.value:
            emoji = '❌'
        elif application.status == ApplicationStatus.WITHDRAWN.value:
            emoji = '🗑️'
        application_title = '{} {}'.format(emoji, applicant_name)

        if message.author == application.user:
            return application_title
        else:
            return '{} / {}'.format(application_title, author_name)

    if type == 'issue':
        issue = conversation.target
        if message.author == issue.affected_user:
            return '☹️ {}'.format(author_name)
        return '☹️ {} / {}'.format(issue.affected_user, author_name)

    if type == 'offer':
        offer = conversation.target
        return '🎁️ {} / {}'.format(offer.name, author_name)

    return author_name


def notify_message_push_subscribers_with_language(message, subscriptions, language):
    conversation = message.conversation

    if not translation.check_for_language(language):
        language = 'en'

    with translation.override(language):
        message_title = get_message_title(message, language)

    notify_subscribers(
        subscriptions=subscriptions,
        title=message_title,
        body=Truncator(message.content).chars(num=1000),
        url=frontend_urls.message_url(message),
        image_url=frontend_urls.user_photo_url(message.author),
        # this causes each notification for a given conversation to replace previous notifications
        # fancier would be to make the new notifications show a summary not just the latest message
        tag='conversation:{}'.format(conversation.id),
    )


@db_task()
def notify_mention_push_subscribers(mention):
    message = mention.message
    conversation = message.conversation
    user = mention.user

    # check (again) they are *not* in the conversation... (as will already get a push message for that case)
    if conversation.conversationparticipant_set.filter(user=user).exists():
        return

    notify_message_push_subscribers_with_language(
        message, WebPushSubscription.objects.filter(user=user), user.language
    )


@db_task()
def notify_new_offer_push_subscribers(offer):

    users = offer.group.members.filter(
        groupmembership__in=GroupMembership.objects.active().with_notification_type(GroupNotificationType.NEW_OFFER),
    )

    subscriptions = WebPushSubscription.objects.filter(
        user__in=users,
    ).\
        exclude(user=offer.user). \
        select_related('user'). \
        order_by('user__language'). \
        distinct()

    for (language, subscriptions) in groupby(subscriptions, key=lambda subscription: subscription.user.language):
        subscriptions = list(subscriptions)
        notify_new_offer_push_subscribers_with_language(offer, subscriptions, language)


def notify_new_offer_push_subscribers_with_language(offer, subscriptions, language):
    if not translation.check_for_language(language):
        language = 'en'

    with translation.override(language):
        message_title = '🎁️ {} / {}'.format(offer.name, offer.user.display_name)

    notify_subscribers(
        subscriptions=subscriptions,
        title=message_title,
        body=Truncator(offer.description).chars(num=1000),
        url=frontend_urls.offer_url(offer),
        # this causes each notification for a given conversation to replace previous notifications
        # fancier would be to make the new notifications show a summary not just the latest message
        tag='offer:{}'.format(offer.id),
    )


@db_periodic_task(crontab(hour='*/24', minute=35))  # every 24 hours
def delete_old_channel_subscriptions():
    with timer() as t:
        # delete old channel subscriptions after some minutes of inactivity
        ChannelSubscription.objects.old().delete()

    stats_utils.periodic_task('subscriptions__delete_old_channel_subscriptions', seconds=t.elapsed_seconds)
