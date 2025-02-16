from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.db.models import Sum, Count, Prefetch
from django.utils import timezone
from enum import Enum

from karrot.base.base_models import BaseModel
from karrot.issues import stats, signals
from karrot.conversations.models import ConversationMixin
from karrot.history.models import History, HistoryTypus
from karrot.utils import markdown


class IssueTypes(Enum):
    CONFLICT_RESOLUTION = 'conflict_resolution'


class IssueStatus(Enum):
    ONGOING = 'ongoing'
    DECIDED = 'decided'
    CANCELLED = 'cancelled'


class IssueQuerySet(models.QuerySet):
    def ongoing(self):
        return self.filter(status=IssueStatus.ONGOING.value)

    def decided(self):
        return self.filter(status=IssueStatus.DECIDED.value)

    def cancelled(self):
        return self.filter(status=IssueStatus.CANCELLED.value)

    def prefetch_for_serializer(self, user):
        return self.prefetch_related(
            Prefetch('votings', Voting.objects.annotate_participant_count()),
            'votings__options',
            Prefetch('votings__options__votes', Vote.objects.filter(user=user), to_attr='your_votes'),
        )


class Issue(BaseModel, ConversationMixin):
    objects = IssueQuerySet.as_manager()

    group = models.ForeignKey('groups.Group', on_delete=models.CASCADE, related_name='issues')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='issues_created')
    status = models.TextField(
        default=IssueStatus.ONGOING.value,
        choices=[(status.value, status.value) for status in IssueStatus],
    )
    status_changed_at = models.DateTimeField(null=True)
    type = models.TextField(
        default=IssueTypes.CONFLICT_RESOLUTION.value,
        choices=[(status.value, status.value) for status in IssueTypes],
    )
    topic = models.TextField()
    affected_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE, related_name='affected_by_issue'
    )

    @property
    def ended_at(self):
        if self.status == IssueStatus.ONGOING.value:
            return None
        return self.status_changed_at

    def decide(self):
        self.status = IssueStatus.DECIDED.value
        self.status_changed_at = timezone.now()
        self.save()

    def cancel(self):
        self.status = IssueStatus.CANCELLED.value
        self.status_changed_at = timezone.now()
        self.save()
        signals.issue_changed.send(sender=self.__class__, issue=self)

    def save(self, **kwargs):
        created = self.pk is None
        super().save(**kwargs)

        if self.votings.count() == 0:
            voting = self.votings.create()
            voting.create_options()

        if created:
            stats.issue_created(self)
            signals.issue_changed.send(sender=self.__class__, issue=self)

    def latest_voting(self):
        return self.votings.latest('created_at')

    def topic_rendered(self, **kwargs):
        return markdown.render(self.topic, **kwargs)

    def is_decided(self):
        return self.status == IssueStatus.DECIDED.value

    def is_ongoing(self):
        return self.status == IssueStatus.ONGOING.value

    def is_cancelled(self):
        return self.status == IssueStatus.CANCELLED.value


class VotingQuerySet(models.QuerySet):
    def due_soon(self):
        in_some_hours = timezone.now() + relativedelta(hours=settings.VOTING_DUE_SOON_HOURS)
        return self.filter(expires_at__gt=timezone.now(), expires_at__lt=in_some_hours)

    def annotate_participant_count(self):
        return self.annotate(_participant_count=Count('options__votes__user', distinct=True))


def voting_expiration_time():
    return timezone.now() + relativedelta(days=settings.VOTING_DURATION_DAYS)


class Voting(BaseModel):
    objects = VotingQuerySet.as_manager()

    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name='votings')
    expires_at = models.DateTimeField(default=voting_expiration_time)
    accepted_option = models.ForeignKey(
        'Option',
        on_delete=models.SET_NULL,
        null=True,
        related_name='accepted_for_voting',
    )

    def is_expired(self):
        return self.expires_at < timezone.now()

    def participant_count(self) -> int:
        if hasattr(self, '_participant_count'):
            return self._participant_count

        return get_user_model().objects.filter(votes_given__option__voting=self).distinct().count()

    def create_options(self):
        options = [
            {
                'type': OptionTypes.FURTHER_DISCUSSION.value,
            },
            {
                'type': OptionTypes.NO_CHANGE.value,
            },
            {
                'type': OptionTypes.REMOVE_USER.value,
            },
        ]
        for option in options:
            self.options.create(**option)

    def save_votes(self, user, vote_data):
        votes = {vote.option_id: vote for vote in Vote.objects.filter(option__voting=self, user=user)}

        created = []
        existing = []
        for option_id, data in vote_data.items():
            vote = votes.get(option_id, None)
            if vote is not None:
                if vote.score == data['score']:
                    existing.append(vote)
                    continue
                vote.delete()
            data['user'] = user
            created.append(Vote.objects.create(**data))

        if len(votes) == 0:
            stats.voted(self.issue)
        elif len(created) > 0:
            stats.vote_changed(self.issue)

        if len(created) > 0:
            signals.issue_changed.send(sender=self.__class__, issue=self.issue)

        return created + existing

    def delete_votes(self, user):
        deleted_rows, _ = Vote.objects.filter(option__voting=self, user=user).delete()
        deleted = deleted_rows > 0

        if deleted:
            stats.vote_deleted(self.issue)
            signals.issue_changed.send(sender=self.__class__, issue=self.issue)

        return deleted

    def calculate_results(self):
        options = list(self.options.annotate(_sum_score=Sum('votes__score')).order_by('_sum_score'))
        for option in options:
            option.sum_score = option._sum_score
            option.save()

        accepted_option = options[-1]
        # if two option have the same highest score, we have a tie and choose further discussion
        if options[-2].sum_score == accepted_option.sum_score:
            accepted_option = next(o for o in options if o.type == OptionTypes.FURTHER_DISCUSSION.value)

        self.accepted_option = accepted_option
        self.save()

        self.accepted_option.do_action()


class OptionTypes(Enum):
    FURTHER_DISCUSSION = 'further_discussion'
    NO_CHANGE = 'no_change'
    REMOVE_USER = 'remove_user'


class Option(BaseModel):
    voting = models.ForeignKey(Voting, on_delete=models.CASCADE, related_name='options')
    type = models.TextField(choices=[(status.value, status.value) for status in OptionTypes])
    sum_score = models.FloatField(null=True)

    def do_action(self):
        if self.type != OptionTypes.FURTHER_DISCUSSION.value:
            self.voting.issue.decide()

        if self.type == OptionTypes.FURTHER_DISCUSSION.value:
            self._further_discussion()
        elif self.type == OptionTypes.REMOVE_USER.value:
            self._remove_user()

        signals.issue_changed.send(sender=self.__class__, issue=self.voting.issue)

    def _further_discussion(self):
        new_voting = self.voting.issue.votings.create()
        for option in self.voting.options.all():
            new_voting.options.create(type=option.type)

    def _remove_user(self):
        issue = self.voting.issue
        group = issue.group
        affected_user = issue.affected_user
        membership = group.groupmembership_set.get(user=affected_user)
        membership.delete()
        History.objects.create(typus=HistoryTypus.MEMBER_REMOVED, group=group, users=[affected_user])


class Vote(BaseModel):
    class Meta:
        unique_together = ('user', 'option')

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='votes_given')
    option = models.ForeignKey(Option, on_delete=models.CASCADE, related_name='votes')
    score = models.IntegerField()
