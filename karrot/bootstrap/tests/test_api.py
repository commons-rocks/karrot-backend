from unittest.mock import ANY, patch

from django.conf import settings
from django.test import override_settings
from geoip2.errors import AddressNotFoundError
from rest_framework import status
from rest_framework.test import APITestCase

from karrot.groups.factories import GroupFactory
from karrot.users.factories import UserFactory
from karrot.utils.geoip import ip_to_city
from karrot.utils.tests.fake import faker

DEFAULT_SETTINGS = {
    'SENTRY_CLIENT_DSN': settings.SENTRY_CLIENT_DSN,
    'SENTRY_ENVIRONMENT': settings.SENTRY_ENVIRONMENT,
    'FILE_UPLOAD_MAX_SIZE': settings.FILE_UPLOAD_MAX_SIZE,
    'FORUM_BANNER_TOPIC_ID': settings.FORUM_BANNER_TOPIC_ID,
    'FORUM_DISCUSSIONS_FEED': settings.FORUM_DISCUSSIONS_FEED,
    'VAPID_PUBLIC_KEY': settings.VAPID_PUBLIC_KEY,
}

OVERRIDE_SETTINGS = {
    'SENTRY_CLIENT_DSN': faker.name(),
    'SENTRY_ENVIRONMENT': faker.name(),
    'FILE_UPLOAD_MAX_SIZE': 12345,
    'FORUM_BANNER_TOPIC_ID': 444,
    'FORUM_DISCUSSIONS_FEED': 'something/else.json',
    'VAPID_PUBLIC_KEY': faker.uuid4(),
}


class TestConfigAPI(APITestCase):
    def test_default_config(self):
        response = self.client.get('/api/config/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data, {
                'sentry': {
                    'dsn': DEFAULT_SETTINGS['SENTRY_CLIENT_DSN'],
                    'environment': DEFAULT_SETTINGS['SENTRY_ENVIRONMENT'],
                },
                'upload': {
                    'max_size': DEFAULT_SETTINGS['FILE_UPLOAD_MAX_SIZE'],
                },
                'forum': {
                    'banner_topic_id': DEFAULT_SETTINGS['FORUM_BANNER_TOPIC_ID'],
                    'discussions_feed': DEFAULT_SETTINGS['FORUM_DISCUSSIONS_FEED'],
                },
                'feedback_possible_days': 30,
                'web_push': {
                    'vapid_public_key': DEFAULT_SETTINGS['VAPID_PUBLIC_KEY'],
                },
            }, response.data
        )

    @override_settings(**OVERRIDE_SETTINGS)
    def test_config_with_overrides(self):
        response = self.client.get('/api/config/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data, {
                'sentry': {
                    'dsn': OVERRIDE_SETTINGS['SENTRY_CLIENT_DSN'],
                    'environment': OVERRIDE_SETTINGS['SENTRY_ENVIRONMENT'],
                },
                'upload': {
                    'max_size': OVERRIDE_SETTINGS['FILE_UPLOAD_MAX_SIZE'],
                },
                'forum': {
                    'banner_topic_id': OVERRIDE_SETTINGS['FORUM_BANNER_TOPIC_ID'],
                    'discussions_feed': OVERRIDE_SETTINGS['FORUM_DISCUSSIONS_FEED'],
                },
                'feedback_possible_days': 30,
                'web_push': {
                    'vapid_public_key': OVERRIDE_SETTINGS['VAPID_PUBLIC_KEY'],
                },
            }, response.data
        )


class TestBootstrapAPI(APITestCase):
    def setUp(self):
        self.user = UserFactory()
        self.member = UserFactory()
        self.group = GroupFactory(members=[self.member], application_questions='')
        self.url = '/api/bootstrap/'
        self.client_ip = '2003:d9:ef08:4a00:4b7a:7964:8a3c:a33e'
        ip_to_city.cache_clear()  # prevent getting cached mock values

    def tearDown(self):
        ip_to_city.cache_clear()

    def test_as_anon(self):
        with self.assertNumQueries(1):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['server'], ANY)
        self.assertEqual(response.data['config'], ANY)
        self.assertEqual(response.data['user'], None)
        self.assertEqual(response.data['geoip'], None)
        self.assertEqual(response.data['groups'], ANY)

    @patch('karrot.utils.geoip.geoip')
    def test_with_geoip(self, geoip):
        lat_lng = [float(val) for val in faker.latlng()]
        city = {'latitude': lat_lng[0], 'longitude': lat_lng[1], 'country_code': 'AA', 'time_zone': 'Europe/Berlin'}
        geoip.city.return_value = city
        response = self.client.get(self.url, HTTP_X_FORWARDED_FOR=self.client_ip)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            dict(response.data['geoip']), {
                'lat': city['latitude'],
                'lng': city['longitude'],
                'country_code': city['country_code'],
                'timezone': city['time_zone'],
            }
        )

    @patch('karrot.utils.geoip.geoip')
    def test_without_geoip(self, geoip):
        geoip.city.side_effect = AddressNotFoundError('not found')
        response = self.client.get(self.url, HTTP_X_FORWARDED_FOR=self.client_ip)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data['geoip'])

    def test_when_logged_in(self):
        self.client.force_login(user=self.user)
        with self.assertNumQueries(2):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['user']['id'], self.user.id)

    def test_can_specify_selected_fields(self):
        self.client.force_login(user=self.user)
        response = self.client.get(self.url, {'fields': 'places,activity_types'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(list(response.data.keys()), ['places', 'activity_types'])

    def test_can_specify_all_fields(self):
        self.client.force_login(user=self.user)
        fields = 'server,config,geoip,user,groups,places,users,status,activity_types'
        response = self.client.get(self.url, {'fields': fields})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(sorted(list(response.data.keys())), sorted(fields.split(',')))

    def test_complains_for_invalid_fields(self):
        self.client.force_login(user=self.user)
        response = self.client.get(self.url, {'fields': 'notafield,orthisone'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
