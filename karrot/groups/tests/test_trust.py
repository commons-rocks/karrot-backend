from unittest.mock import ANY

from dateutil.relativedelta import relativedelta
from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.reverse import reverse
from rest_framework.test import APITestCase

from karrot.groups.factories import GroupFactory
from karrot.groups.models import GroupMembership, Trust
from karrot.groups.roles import GROUP_EDITOR
from karrot.history.models import History, HistoryTypus
from karrot.users.factories import UserFactory


class TestTrustThreshold(TestCase):
    def create_group_with_members(self, member_count):
        self.members = [UserFactory() for _ in range(member_count)]
        self.group = GroupFactory(members=self.members)
        # trust threshold calculation ignores recently joined users, so we need to create users before that
        two_days_ago = timezone.now() - relativedelta(days=2)
        GroupMembership.objects.filter(group=self.group).update(created_at=two_days_ago)

    def test_min_threshold(self):
        self.create_group_with_members(1)
        self.assertEqual(
            self.group.trust_threshold_for_newcomer(),
            1,
        )

    def test_ramp_up_threshold(self):
        self.create_group_with_members(5)
        self.assertEqual(
            self.group.trust_threshold_for_newcomer(),
            2,
        )

    def test_max_threshold(self):
        self.create_group_with_members(6)
        self.assertEqual(
            self.group.trust_threshold_for_newcomer(),
            3,
        )

    def test_ignores_recently_joined_users(self):
        self.create_group_with_members(1)
        [self.group.add_member(UserFactory()) for _ in range(5)]
        self.assertEqual(
            self.group.trust_threshold_for_newcomer(),
            1,
        )


class TestTrustReceiver(TestCase):
    def test_newcomer_becomes_editor(self):
        editor = UserFactory()
        newcomer = UserFactory()
        group = GroupFactory(members=[editor], newcomers=[newcomer])
        two_days_ago = timezone.now() - relativedelta(days=2)
        GroupMembership.objects.filter(group=group).update(created_at=two_days_ago)
        mail.outbox = []

        membership = GroupMembership.objects.get(user=newcomer, group=group)
        Trust.objects.create(membership=membership, given_by=editor)

        self.assertTrue(group.is_editor(newcomer))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('You gained editing permissions', mail.outbox[0].subject)

        self.assertEqual(History.objects.filter(typus=HistoryTypus.MEMBER_BECAME_EDITOR).count(), 1)

    def test_do_not_send_notification_again(self):
        editor = UserFactory()
        editor2 = UserFactory()
        group = GroupFactory(members=[editor, editor2])
        two_days_ago = timezone.now() - relativedelta(days=2)
        GroupMembership.objects.filter(group=group).update(created_at=two_days_ago)
        mail.outbox = []

        membership = GroupMembership.objects.get(user=editor, group=group)
        Trust.objects.create(membership=membership, given_by=editor2)

        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(History.objects.filter(typus=HistoryTypus.MEMBER_BECAME_EDITOR).count(), 0)

    def test_editor_loses_editor_role(self):
        editor1 = UserFactory()
        editor2 = UserFactory()
        group = GroupFactory(members=[editor1, editor2], newcomers=[])
        two_days_ago = timezone.now() - relativedelta(days=2)
        GroupMembership.objects.filter(group=group).update(created_at=two_days_ago)
        membership = GroupMembership.objects.get(user=editor1, group=group)
        trust = Trust.objects.create(membership=membership, given_by=editor2)

        mail.outbox = []

        trust.delete()

        self.assertFalse(group.is_editor(editor1))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('You lost editing permissions', mail.outbox[0].subject)

        self.assertEqual(History.objects.filter(typus=HistoryTypus.USER_LOST_EDITOR_ROLE).count(), 1)

    def test_remove_trust_when_giver_leaves_group(self):
        editor = UserFactory()
        newcomer = UserFactory()
        group = GroupFactory(members=[editor], newcomers=[newcomer])
        membership = GroupMembership.objects.get(user=newcomer, group=group)
        Trust.objects.create(membership=membership, given_by=editor)

        group.remove_member(editor)

        self.assertEqual(0, Trust.objects.filter(membership=membership).count())

    def test_do_not_remove_trust_in_other_groups(self):
        editor = UserFactory()
        newcomer = UserFactory()
        group = GroupFactory(members=[editor], newcomers=[newcomer])
        membership = GroupMembership.objects.get(user=newcomer, group=group)
        other_group = GroupFactory(members=[editor])
        Trust.objects.create(membership=membership, given_by=editor)

        other_group.remove_member(editor)

        self.assertEqual(1, Trust.objects.filter(membership=membership).count())


class TestTrustAPI(APITestCase):
    def setUp(self):
        self.member1 = UserFactory()
        self.member2 = UserFactory()
        self.group = GroupFactory(members=[self.member1, self.member2])

    def test_give_trust(self):
        self.client.force_login(user=self.member1)

        url = reverse('group-trust-user', args=(self.group.id, self.member2.id))
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            Trust.objects.filter(
                membership__group=self.group,
                membership__user=self.member2,
                given_by=self.member1,
                role=GROUP_EDITOR,
            ).exists()
        )

    def test_give_trust_for_role(self):
        self.group.roles.create(name='someotherrole', description='nothing')
        self.group.save()
        self.client.force_login(user=self.member1)
        mail.outbox = []

        url = reverse('group-trust-user', args=(self.group.id, self.member2.id))
        response = self.client.post(url, {'role': 'someotherrole'})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            Trust.objects.filter(
                membership__group=self.group,
                membership__user=self.member2,
                given_by=self.member1,
                role='someotherrole',
            ).exists()
        )

        self.assertIn('You received a new role in', mail.outbox[0].subject)

    def test_give_trust_for_role_with_role_requirement(self):
        self.group.roles.create(name='special_role', description='nothing', role_required_for_trust='editor')
        self.group.save()
        newcomer = UserFactory()
        self.group.add_member(newcomer)
        self.client.force_login(user=newcomer)

        url = reverse('group-trust-user', args=(self.group.id, self.member2.id))
        response = self.client.post(url, {'role': 'special_role'})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertEqual(
            response.data['role'][0],
            'Trust for role "special_role" can only be given by users with "editor" role',
        )
        self.assertFalse(
            Trust.objects.filter(
                membership__group=self.group,
                membership__user=self.member2,
                given_by=self.member1,
                role='special_role',
            ).exists()
        )

    def test_can_only_give_trust_once(self):
        membership = GroupMembership.objects.get(user=self.member2, group=self.group)
        Trust.objects.create(membership=membership, given_by=self.member1)
        self.client.force_login(user=self.member1)

        url = reverse('group-trust-user', args=(self.group.id, self.member2.id))
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_give_trust_to_self(self):
        self.client.force_login(user=self.member1)

        url = reverse('group-trust-user', args=(self.group.id, self.member1.id))
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            Trust.objects.filter(
                membership__group=self.group,
                membership__user=self.member2,
                given_by=self.member1,
            ).exists()
        )

    def test_trust_can_be_revoked(self):
        membership = GroupMembership.objects.get(user=self.member2, group=self.group)
        Trust.objects.create(membership=membership, given_by=self.member1)
        self.client.force_login(user=self.member1)

        url = reverse('group-trust-user', args=(self.group.id, self.member2.id))
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(
            Trust.objects.filter(
                membership__group=self.group,
                membership__user=self.member2,
                given_by=self.member1,
            ).exists()
        )

    def test_trust_can_be_revoked_for_role(self):
        self.group.roles.create(name='someotherrole', description='nothing')
        self.group.save()
        membership = GroupMembership.objects.get(user=self.member2, group=self.group)
        Trust.objects.create(membership=membership, given_by=self.member1, role=GROUP_EDITOR)
        Trust.objects.create(membership=membership, given_by=self.member1, role='someotherrole')
        self.client.force_login(user=self.member1)

        url = reverse('group-trust-user', args=(self.group.id, self.member2.id))
        response = self.client.delete(url, {'role': 'someotherrole'})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(
            Trust.objects.filter(
                membership__group=self.group,
                membership__user=self.member2,
                given_by=self.member1,
                role='someotherrole',
            ).exists()
        )
        self.assertTrue(
            Trust.objects.filter(
                membership__group=self.group,
                membership__user=self.member2,
                given_by=self.member1,
                role=GROUP_EDITOR,
            ).exists()
        )

    def test_trust_that_has_not_been_given_cannot_be_revoked(self):
        GroupMembership.objects.get(user=self.member2, group=self.group)
        self.client.force_login(user=self.member1)

        url = reverse('group-trust-user', args=(self.group.id, self.member2.id))
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class TestTrustList(APITestCase):
    def setUp(self):
        self.member1 = UserFactory()
        self.member2 = UserFactory()
        self.member3 = UserFactory()
        self.group = GroupFactory(members=[self.member1, self.member2, self.member3])

        membership = GroupMembership.objects.get(user=self.member2, group=self.group)
        Trust.objects.create(membership=membership, given_by=self.member1)
        membership = GroupMembership.objects.get(user=self.member1, group=self.group)
        Trust.objects.create(membership=membership, given_by=self.member2)
        self.group.roles.create(name='anotherrole', description='nothing')
        Trust.objects.create(membership=membership, given_by=self.member3, role='anotherrole')

    def test_list_trust_for_group(self):
        self.client.force_login(user=self.member1)
        response = self.client.get('/api/groups/{}/'.format(self.group.id))
        self.assertEqual(
            response.data['memberships'][self.member1.id]['trust'], [
                {
                    'created_at': ANY,
                    'given_by': self.member2.id,
                    'role': GROUP_EDITOR
                },
                {
                    'created_at': ANY,
                    'given_by': self.member3.id,
                    'role': 'anotherrole'
                },
            ]
        )
        self.assertEqual(
            response.data['memberships'][self.member2.id]['trust'], [
                {
                    'created_at': ANY,
                    'given_by': self.member1.id,
                    'role': GROUP_EDITOR
                },
            ]
        )

    def test_backwards_compatible_trusted_by_field(self):
        self.client.force_login(user=self.member1)
        response = self.client.get('/api/groups/{}/'.format(self.group.id))
        # trust for other roles won't be included in the 'trusted_by' field
        self.assertEqual(response.data['memberships'][self.member1.id]['trusted_by'], [self.member2.id])
        self.assertEqual(response.data['memberships'][self.member2.id]['trusted_by'], [self.member1.id])
