"""Unit tests for settings utility helpers."""

from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from enterprise_access.settings.utils import get_env_setting, get_logger_config


class TestSettingsUtils(SimpleTestCase):
    """Tests for enterprise_access.settings.utils."""

    def test_get_env_setting_returns_value(self):
        with mock.patch.dict('enterprise_access.settings.utils.environ', {'MY_SETTING': 'value'}, clear=True):
            self.assertEqual(get_env_setting('MY_SETTING'), 'value')

    def test_get_env_setting_raises_when_missing(self):
        with mock.patch.dict('enterprise_access.settings.utils.environ', {}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                get_env_setting('MISSING_SETTING')

    @mock.patch('enterprise_access.settings.utils.platform.node', return_value='host1.domain')
    def test_get_logger_config_default_values(self, _mock_node):
        logger_config = get_logger_config(logging_env='test', debug=False)

        self.assertEqual(logger_config['version'], 1)
        self.assertEqual(logger_config['handlers']['console']['level'], 'INFO')
        self.assertIn('host1', logger_config['formatters']['syslog_format']['format'])

    @mock.patch('enterprise_access.settings.utils.platform.node', return_value='host2.domain')
    def test_get_logger_config_debug_and_custom_format(self, _mock_node):
        logger_config = get_logger_config(
            logging_env='stage',
            debug=True,
            service_variant='custom-service',
            format_string='%(levelname)s %(message)s',
        )

        self.assertEqual(logger_config['handlers']['console']['level'], 'DEBUG')
        self.assertEqual(logger_config['formatters']['standard']['format'], '%(levelname)s %(message)s')
        self.assertIn('custom-service', logger_config['formatters']['syslog_format']['format'])
