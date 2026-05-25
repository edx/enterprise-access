"""
Unit tests for the utility functions.
"""
from unittest import mock
from unittest.mock import ANY
from uuid import uuid4

from confluent_kafka import KafkaError, KafkaException
from confluent_kafka.error import ValueSerializationError
from django.conf import settings
from django.test import TestCase
from django.test.utils import override_settings
from faker import Faker

from enterprise_access.apps.events.signals import ACCESS_POLICY_CREATED, SUBSIDY_REDEEMED
from enterprise_access.apps.events.utils import (
    ProducerFactory,
    create_topics,
    send_access_policy_event_to_event_bus,
    send_coupon_code_request_event_to_event_bus,
    send_subsidy_redemption_event_to_event_bus,
    verify_event
)
from enterprise_access.apps.subsidy_access_policy.models import AccessMethods

FAKER = Faker()


class UtilsTests(TestCase):
    """
    Unit tests for the utility functions.
    """
    @override_settings(KAFKA_ENABLED=True)
    @mock.patch('enterprise_access.apps.events.utils.logger', return_value=mock.MagicMock())
    @mock.patch('enterprise_access.apps.events.utils.SerializingProducer', return_value=mock.MagicMock())
    def test_send_access_policy_event_to_event_bus(self, mock_serializing_producer, mock_logger):
        """
        Validate the behavior of send_access_policy_event_to_event_bus utility method.
        """
        access_policy_event_data = {
            'uuid': uuid4(),
            'active': True,
            'subsidy_uuid': uuid4(),
            'access_method': AccessMethods.DIRECT,
        }

        send_access_policy_event_to_event_bus(ACCESS_POLICY_CREATED.event_type, access_policy_event_data)

        mock_serializing_producer().produce.assert_any_call(
            settings.ACCESS_POLICY_TOPIC_NAME,
            key=str(ACCESS_POLICY_CREATED.event_type),
            value=ANY,
            on_delivery=verify_event
        )

        assert mock_serializing_producer().poll.call_count == 1

        mock_serializing_producer().poll.side_effect = ValueSerializationError

        send_access_policy_event_to_event_bus(ACCESS_POLICY_CREATED.event_type, access_policy_event_data)

        mock_serializing_producer().produce.assert_any_call(
            settings.ACCESS_POLICY_TOPIC_NAME,
            key=str(ACCESS_POLICY_CREATED.event_type),
            value=ANY,
            on_delivery=verify_event
        )
        assert mock_logger.exception.call_count == 1

    @override_settings(KAFKA_ENABLED=True)
    @mock.patch('enterprise_access.apps.events.utils.logger', return_value=mock.MagicMock())
    @mock.patch('enterprise_access.apps.events.utils.SerializingProducer', return_value=mock.MagicMock())
    def test_send_subsidy_redemption_event_to_event_bus(self, mock_serializing_producer, mock_logger):
        """
        Validate the behavior of send_access_policy_event_to_event_bus utility method.
        """
        subsidy_redemption_event_data = {
            'enterprise_uuid': uuid4(),
            'content_key': 'test-course',
            'lms_user_id': FAKER.pyint(),
        }

        send_subsidy_redemption_event_to_event_bus(SUBSIDY_REDEEMED.event_type, subsidy_redemption_event_data)

        mock_serializing_producer().produce.assert_any_call(
            settings.SUBSIDY_REDEMPTION_TOPIC_NAME,
            key=str(SUBSIDY_REDEEMED.event_type),
            value=ANY,
            on_delivery=verify_event
        )

        assert mock_serializing_producer().poll.call_count == 1

        mock_serializing_producer().poll.side_effect = ValueSerializationError

        send_subsidy_redemption_event_to_event_bus(SUBSIDY_REDEEMED.event_type, subsidy_redemption_event_data)

        mock_serializing_producer().produce.assert_any_call(
            settings.SUBSIDY_REDEMPTION_TOPIC_NAME,
            key=str(SUBSIDY_REDEEMED.event_type),
            value=ANY,
            on_delivery=verify_event
        )
        assert mock_logger.exception.call_count == 1

    @mock.patch('enterprise_access.apps.events.utils.logger', return_value=mock.MagicMock())
    @mock.patch('enterprise_access.apps.events.utils.SerializingProducer', return_value=mock.MagicMock())
    def test_send_coupon_code_request_event_to_event_bus(self, mock_serializing_producer, mock_logger):
        """Validate coupon code event production and serialization error handling."""
        coupon_request_event_data = {
            'uuid': str(uuid4()),
            'lms_user_id': 11,
            'course_id': 'course-v1:edX+DemoX+Demo_Course',
            'enterprise_customer_uuid': str(uuid4()),
            'state': 'requested',
            'reviewed_at': None,
            'reviewer_lms_user_id': None,
            'coupon_id': None,
            'coupon_code': None,
        }

        send_coupon_code_request_event_to_event_bus('coupon-code-requested', coupon_request_event_data)

        mock_serializing_producer().produce.assert_any_call(
            settings.COUPON_CODE_REQUEST_TOPIC_NAME,
            key='coupon-code-requested',
            value=ANY,
            on_delivery=verify_event
        )
        assert mock_serializing_producer().poll.call_count == 1

        mock_serializing_producer().poll.side_effect = ValueSerializationError
        send_coupon_code_request_event_to_event_bus('coupon-code-requested', coupon_request_event_data)
        assert mock_logger.exception.call_count == 1

    def test_verify_event_logs_warning_when_error_exists(self):
        error = ValueError('failed')

        with mock.patch('enterprise_access.apps.events.utils.logger') as mock_logger:
            verify_event(error, mock.Mock())

        mock_logger.warning.assert_called_once()

    def test_verify_event_logs_info_when_event_delivered(self):
        event = mock.Mock()
        event.topic.return_value = 'topic-name'
        event.key.return_value = b'key'
        event.partition.return_value = 1

        with mock.patch('enterprise_access.apps.events.utils.logger') as mock_logger:
            verify_event(None, event)

        mock_logger.info.assert_called_once()


class ProducerFactoryTests(TestCase):
    """Unit tests for ProducerFactory caching and initialization."""

    def tearDown(self):
        super().tearDown()
        ProducerFactory._type_to_producer = {}

    @override_settings(KAFKA_BOOTSTRAP_SERVER='localhost:9092', KAFKA_API_KEY='', KAFKA_API_SECRET='')
    @mock.patch('enterprise_access.apps.events.utils.SerializingProducer')
    def test_get_or_create_event_producer_creates_and_caches(self, mock_serializing_producer):
        mock_producer = mock.Mock()
        mock_serializing_producer.return_value = mock_producer

        producer = ProducerFactory.get_or_create_event_producer('event_type', mock.Mock(), mock.Mock())

        self.assertIs(producer, mock_producer)
        self.assertEqual(ProducerFactory._type_to_producer['event_type'], mock_producer)
        mock_serializing_producer.assert_called_once()

    @override_settings(KAFKA_BOOTSTRAP_SERVER='localhost:9092', KAFKA_API_KEY='key', KAFKA_API_SECRET='secret')
    @mock.patch('enterprise_access.apps.events.utils.SerializingProducer')
    def test_get_or_create_event_producer_includes_auth_when_configured(self, mock_serializing_producer):
        ProducerFactory.get_or_create_event_producer('event_type', mock.Mock(), mock.Mock())

        call_args = mock_serializing_producer.call_args[0][0]
        self.assertEqual(call_args['security.protocol'], 'SASL_SSL')
        self.assertEqual(call_args['sasl.username'], 'key')
        self.assertEqual(call_args['sasl.password'], 'secret')

    @override_settings(KAFKA_BOOTSTRAP_SERVER='localhost:9092', KAFKA_API_KEY='', KAFKA_API_SECRET='')
    @mock.patch('enterprise_access.apps.events.utils.SerializingProducer')
    def test_get_or_create_event_producer_returns_existing_without_recreating(self, mock_serializing_producer):
        existing_producer = mock.Mock()
        ProducerFactory._type_to_producer['event_type'] = existing_producer

        producer = ProducerFactory.get_or_create_event_producer('event_type', mock.Mock(), mock.Mock())

        self.assertIs(producer, existing_producer)
        mock_serializing_producer.assert_not_called()


class CreateTopicsTests(TestCase):
    """Unit tests for topic creation utility."""

    @override_settings(
        KAFKA_BOOTSTRAP_SERVER='localhost:9092',
        KAFKA_API_KEY='',
        KAFKA_API_SECRET='',
        KAFKA_PARTITIONS_PER_TOPIC=1,
        KAFKA_REPLICATION_FACTOR_PER_TOPIC=1,
    )
    @mock.patch('enterprise_access.apps.events.utils.NewTopic')
    @mock.patch('enterprise_access.apps.events.utils.AdminClient')
    def test_create_topics_success_path(self, mock_admin_client, mock_new_topic):
        future = mock.Mock()
        future.result.return_value = None
        admin_client_instance = mock.Mock()
        admin_client_instance.create_topics.return_value = {'topic-a': future}
        mock_admin_client.return_value = admin_client_instance

        create_topics(['topic-a'])

        mock_admin_client.assert_called_once_with({'bootstrap.servers': 'localhost:9092'})
        mock_new_topic.assert_called_once_with('topic-a', num_partitions=1, replication_factor=1)
        future.result.assert_called_once()

    @override_settings(
        KAFKA_BOOTSTRAP_SERVER='localhost:9092',
        KAFKA_API_KEY='api-key',
        KAFKA_API_SECRET='api-secret',
        KAFKA_PARTITIONS_PER_TOPIC=1,
        KAFKA_REPLICATION_FACTOR_PER_TOPIC=1,
    )
    @mock.patch('enterprise_access.apps.events.utils.NewTopic')
    @mock.patch('enterprise_access.apps.events.utils.AdminClient')
    def test_create_topics_includes_auth_settings_when_configured(self, mock_admin_client, _mock_new_topic):
        future = mock.Mock()
        future.result.return_value = None
        admin_client_instance = mock.Mock()
        admin_client_instance.create_topics.return_value = {'topic-a': future}
        mock_admin_client.return_value = admin_client_instance

        create_topics(['topic-a'])

        admin_config = mock_admin_client.call_args[0][0]
        self.assertEqual(admin_config['security.protocol'], 'SASL_SSL')
        self.assertEqual(admin_config['sasl.username'], 'api-key')
        self.assertEqual(admin_config['sasl.password'], 'api-secret')

    @override_settings(
        KAFKA_BOOTSTRAP_SERVER='localhost:9092',
        KAFKA_API_KEY='',
        KAFKA_API_SECRET='',
        KAFKA_PARTITIONS_PER_TOPIC=1,
        KAFKA_REPLICATION_FACTOR_PER_TOPIC=1,
    )
    @mock.patch('enterprise_access.apps.events.utils.NewTopic')
    @mock.patch('enterprise_access.apps.events.utils.AdminClient')
    def test_create_topics_topic_already_exists(self, mock_admin_client, _mock_new_topic):
        exception = KafkaException(KafkaError(KafkaError.TOPIC_ALREADY_EXISTS))

        future = mock.Mock()
        future.result.side_effect = exception
        admin_client_instance = mock.Mock()
        admin_client_instance.create_topics.return_value = {'topic-a': future}
        mock_admin_client.return_value = admin_client_instance

        with mock.patch('enterprise_access.apps.events.utils.logger') as mock_logger:
            create_topics(['topic-a'])

        mock_logger.info.assert_called()

    @override_settings(
        KAFKA_BOOTSTRAP_SERVER='localhost:9092',
        KAFKA_API_KEY='',
        KAFKA_API_SECRET='',
        KAFKA_PARTITIONS_PER_TOPIC=1,
        KAFKA_REPLICATION_FACTOR_PER_TOPIC=1,
    )
    @mock.patch('enterprise_access.apps.events.utils.NewTopic')
    @mock.patch('enterprise_access.apps.events.utils.AdminClient')
    def test_create_topics_raises_for_unexpected_kafka_error(self, mock_admin_client, _mock_new_topic):
        future = mock.Mock()
        future.result.side_effect = KafkaException(KafkaError(KafkaError.INVALID_CONFIG))
        admin_client_instance = mock.Mock()
        admin_client_instance.create_topics.return_value = {'topic-a': future}
        mock_admin_client.return_value = admin_client_instance

        with self.assertRaises(KafkaException):
            create_topics(['topic-a'])
