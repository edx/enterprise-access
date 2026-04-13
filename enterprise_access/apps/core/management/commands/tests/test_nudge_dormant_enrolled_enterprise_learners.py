"""
Tests for the django management command `nudge_dormant_enrolled_enterprise_learners`.
"""
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.test.utils import override_settings

LOGGER_NAME = 'enterprise_access.apps.core.management.commands.nudge_dormant_enrolled_enterprise_learners'


class NudgeDormantEnrolledEnterpriseLearnersCommandTests(TestCase):
    """
    Test command `nudge_dormant_enrolled_enterprise_learners`.
    """
    command = 'nudge_dormant_enrolled_enterprise_learners'

    @mock.patch('enterprise_access.apps.core.management.commands.nudge_dormant_enrolled_enterprise_learners.track_event')
    @mock.patch(
        'enterprise_access.apps.core.management.commands.nudge_dormant_enrolled_enterprise_learners.Command.'
        'get_query_results_from_reporting_db'
    )
    def test_nudge_dormant_enrolled_enterprise_learners(
            self,
            mock_get_query_results,
            mock_event_track,
    ):
        """
        Test that nudge_dormant_enrolled_enterprise_learners event is sent
        """
        mock_get_query_results.return_value = [list(range(14)) for _ in range(10)]
        with self.assertLogs(LOGGER_NAME, level='INFO') as log:
            call_command(self.command)
            self.assertEqual(mock_event_track.call_count, 10)
            self.assertIn(
                '[Dormant Nudge] Segment event fired for nudge email to dormant enrolled enterprise learners. '
                'LMS User Id: 0, Organization Name: 1, Course Title: 2',
                '\n'.join(log.output),
            )

        mock_event_track.reset_mock()

        with self.assertLogs(LOGGER_NAME, level='INFO') as log:
            call_command(self.command, '--no-commit')
            self.assertEqual(mock_event_track.call_count, 0)
            self.assertIn(
                '[Dormant Nudge] Execution completed.',
                '\n'.join(log.output),
            )

    @mock.patch('enterprise_access.apps.core.management.commands.nudge_dormant_enrolled_enterprise_learners.connections')
    def test_get_query_results_from_reporting_db(self, mock_connections):
        """
        Test get_query_results_from_reporting_db executes and returns all rows.
        """
        command = __import__(
            'enterprise_access.apps.core.management.commands.nudge_dormant_enrolled_enterprise_learners',
            fromlist=['Command'],
        ).Command()

        mock_cursor = mock.MagicMock()
        mock_cursor.fetchall.return_value = [tuple(range(14)), tuple(range(14))]
        mock_connections.__getitem__.return_value.cursor.return_value.__enter__.return_value = mock_cursor

        rows = list(command.get_query_results_from_reporting_db())

        self.assertEqual(rows, [tuple(range(14)), tuple(range(14))])
        mock_cursor.execute.assert_called_once()
        mock_cursor.fetchall.assert_called_once()

    @mock.patch('enterprise_access.apps.core.management.commands.nudge_dormant_enrolled_enterprise_learners.connections')
    @override_settings(DORMANT_NUDGE_REPORT_DB_ALIAS='reporting')
    def test_get_reporting_db_alias_uses_configured_alias_when_available(self, mock_connections):
        """Configured reporting alias is used when present."""
        command = __import__(
            'enterprise_access.apps.core.management.commands.nudge_dormant_enrolled_enterprise_learners',
            fromlist=['Command'],
        ).Command()

        mock_connections.__getitem__.return_value = mock.Mock()

        alias = command._get_reporting_db_alias()

        self.assertEqual(alias, 'reporting')
        mock_connections.__getitem__.assert_called_once_with('reporting')

    @mock.patch('enterprise_access.apps.core.management.commands.nudge_dormant_enrolled_enterprise_learners.connections')
    @override_settings(DORMANT_NUDGE_REPORT_DB_ALIAS='reporting')
    def test_get_reporting_db_alias_falls_back_to_default_when_missing(self, mock_connections):
        """Missing reporting alias falls back to default and logs a warning."""
        module = __import__(
            'enterprise_access.apps.core.management.commands.nudge_dormant_enrolled_enterprise_learners',
            fromlist=['Command', 'ConnectionDoesNotExist'],
        )
        command = module.Command()

        mock_connections.__getitem__.side_effect = [module.ConnectionDoesNotExist('missing'), mock.Mock()]

        with self.assertLogs(LOGGER_NAME, level='WARNING') as log:
            alias = command._get_reporting_db_alias()

        self.assertEqual(alias, 'default')
        self.assertIn('falling back to default', '\n'.join(log.output))

    def test_query_uses_course_progress_and_not_block_count(self):
        """Regression guard for Snowflake schema changes in dormant learner filter."""
        module = __import__(
            'enterprise_access.apps.core.management.commands.nudge_dormant_enrolled_enterprise_learners',
            fromlist=['QUERY'],
        )

        self.assertIn('course_progress', module.QUERY)
        self.assertNotIn('BLOCK_COUNT', module.QUERY)
