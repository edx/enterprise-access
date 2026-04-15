"""
Tests for the django management command `nudge_dormant_enrolled_enterprise_learners`.
"""
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

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

    @mock.patch('enterprise_access.apps.core.management.commands.nudge_dormant_enrolled_enterprise_learners.fetch_all_query_results')
    def test_get_query_results_from_reporting_db(self, mock_fetch_all_query_results):
        """
        Test get_query_results_from_reporting_db executes and returns all rows.
        """
        command = __import__(
            'enterprise_access.apps.core.management.commands.nudge_dormant_enrolled_enterprise_learners',
            fromlist=['Command'],
        ).Command()

        mock_fetch_all_query_results.return_value = [tuple(range(14)), tuple(range(14))]

        rows = list(command.get_query_results_from_reporting_db())

        self.assertEqual(rows, [tuple(range(14)), tuple(range(14))])
        mock_fetch_all_query_results.assert_called_once()

    def test_query_uses_course_progress_and_not_block_count(self):
        """Regression guard for Snowflake schema changes in dormant learner filter."""
        module = __import__(
            'enterprise_access.apps.core.management.commands.nudge_dormant_enrolled_enterprise_learners',
            fromlist=['QUERY'],
        )

        self.assertIn('course_progress', module.QUERY)
        self.assertNotIn('BLOCK_COUNT', module.QUERY)
