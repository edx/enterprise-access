"""
Tests for the django management command `monthly_impact_report`.
"""
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

LOGGER_NAME = 'enterprise_access.apps.core.management.commands.monthly_impact_report'


class MonthlyImpactReportCommandTests(TestCase):
    """
    Test command `monthly_impact_report`.
    """
    command = 'monthly_impact_report'

    @mock.patch('enterprise_access.apps.core.management.commands.monthly_impact_report.track_event')
    @mock.patch(
        'enterprise_access.apps.core.management.commands.monthly_impact_report.Command.get_query_results_from_reporting_db'
    )
    def test_monthly_impact_report(
            self,
            mock_get_query_results,
            mock_event_track,
    ):
        """
        Test that monthly_impact_report event is sent
        """
        mock_get_query_results.return_value = [list(range(50)) for _ in range(10)]
        with self.assertLogs(LOGGER_NAME, level='INFO') as log:
            call_command(self.command)
            self.assertEqual(mock_event_track.call_count, 10)
            self.assertIn(
                '[Monthly Impact Report] Segment event fired for monthly impact report. '
                'lms_user_id: 5, Enterprise Name: 3',
                '\n'.join(log.output),
            )

        mock_event_track.reset_mock()

        with self.assertLogs(LOGGER_NAME, level='INFO') as log:
            call_command(self.command, '--no-commit')
            self.assertEqual(mock_event_track.call_count, 0)
            self.assertIn(
                '[Monthly Impact Report] Execution completed.',
                '\n'.join(log.output),
            )

    @mock.patch('enterprise_access.apps.core.management.commands.monthly_impact_report.fetch_all_query_results')
    def test_get_query_results_from_reporting_db(self, mock_fetch_all_query_results):
        """
        Test get_query_results_from_reporting_db executes and returns all rows.
        """
        command = __import__(
            'enterprise_access.apps.core.management.commands.monthly_impact_report',
            fromlist=['Command'],
        ).Command()

        mock_fetch_all_query_results.return_value = [tuple(range(50)), tuple(range(50))]

        rows = list(command.get_query_results_from_reporting_db())

        self.assertEqual(rows, [tuple(range(50)), tuple(range(50))])
        mock_fetch_all_query_results.assert_called_once()
