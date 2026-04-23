"""
Tests for the django management command `monthly_impact_report`.
"""
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from enterprise_access.apps.core.management.commands.monthly_impact_report import Command

LOGGER_NAME = 'enterprise_access.apps.core.management.commands.monthly_impact_report'

# A row tuple with 50 elements; indices match the positions used in handle().
MOCK_ROW = tuple(range(50))


class GetQueryResultsFromReportingDbTests(TestCase):
    """
    Tests for Command.get_query_results_from_reporting_db().
    """

    @mock.patch(
        'enterprise_access.apps.core.management.commands.monthly_impact_report.fetch_all_query_results'
    )
    def test_returns_all_rows_from_snowflake(self, mock_fetch):
        """
        get_query_results_from_reporting_db should delegate to fetch_all_query_results
        and yield every row it returns.
        """
        mock_fetch.return_value = [MOCK_ROW, MOCK_ROW]

        command = Command()
        rows = list(command.get_query_results_from_reporting_db())

        self.assertEqual(rows, [MOCK_ROW, MOCK_ROW])
        mock_fetch.assert_called_once()

    @mock.patch(
        'enterprise_access.apps.core.management.commands.monthly_impact_report.fetch_all_query_results'
    )
    def test_returns_empty_when_no_rows(self, mock_fetch):
        """
        get_query_results_from_reporting_db returns an empty iterable when there are no results.
        """
        mock_fetch.return_value = []
        command = Command()
        rows = list(command.get_query_results_from_reporting_db())
        self.assertEqual(rows, [])


class EmitEventTests(TestCase):
    """
    Tests for Command.emit_event().
    """

    @mock.patch(
        'enterprise_access.apps.core.management.commands.monthly_impact_report.track_event'
    )
    def test_emit_event_fires_track_event_and_logs(self, mock_track_event):
        """
        emit_event should call track_event with the correct arguments and log a message.
        """
        command = Command()
        kwargs = {
            'EXTERNAL_ID': 42,
            'ENTERPRISE_NAME': 'Acme Corp',
        }

        with self.assertLogs(LOGGER_NAME, level='INFO') as log:
            command.emit_event(**kwargs)

        mock_track_event.assert_called_once_with(
            '42',
            'edx.bi.enterprise.user.admin.impact_report',
            kwargs,
        )
        all_logs = '\n'.join(log.output)
        self.assertIn('lms_user_id: 42', all_logs)
        self.assertIn('Enterprise Name: Acme Corp', all_logs)


class HandleTests(TestCase):
    """
    Tests for Command.handle().
    """
    command = 'monthly_impact_report'

    @mock.patch(
        'enterprise_access.apps.core.management.commands.monthly_impact_report.track_event'
    )
    @mock.patch(
        'enterprise_access.apps.core.management.commands.monthly_impact_report.'
        'Command.get_query_results_from_reporting_db'
    )
    def test_handle_commits_events_for_each_row(self, mock_get_rows, mock_track_event):
        """
        Running the command without --no-commit should emit one Segment event per row.
        """
        mock_get_rows.return_value = [MOCK_ROW for _ in range(5)]

        with self.assertLogs(LOGGER_NAME, level='INFO') as log:
            call_command(self.command)

        self.assertEqual(mock_track_event.call_count, 5)
        all_logs = '\n'.join(log.output)
        self.assertIn('[Monthly Impact Report]  Process started.', all_logs)
        self.assertIn('[Monthly Impact Report] Execution completed.', all_logs)
        self.assertIn('[Monthly Impact Report] Segment event fired for monthly impact report.', all_logs)

    @mock.patch(
        'enterprise_access.apps.core.management.commands.monthly_impact_report.track_event'
    )
    @mock.patch(
        'enterprise_access.apps.core.management.commands.monthly_impact_report.'
        'Command.get_query_results_from_reporting_db'
    )
    def test_handle_no_commit_skips_events(self, mock_get_rows, mock_track_event):
        """
        Running the command with --no-commit should not emit any Segment events.
        """
        mock_get_rows.return_value = [MOCK_ROW for _ in range(5)]

        with self.assertLogs(LOGGER_NAME, level='INFO') as log:
            call_command(self.command, '--no-commit')

        mock_track_event.assert_not_called()
        self.assertIn('[Monthly Impact Report] Execution completed.', '\n'.join(log.output))

    @mock.patch(
        'enterprise_access.apps.core.management.commands.monthly_impact_report.track_event'
    )
    @mock.patch(
        'enterprise_access.apps.core.management.commands.monthly_impact_report.'
        'Command.get_query_results_from_reporting_db'
    )
    def test_handle_no_rows_completes_without_events(self, mock_get_rows, mock_track_event):
        """
        When the reporting DB returns no rows, the command should complete without emitting any events.
        """
        mock_get_rows.return_value = []

        with self.assertLogs(LOGGER_NAME, level='INFO') as log:
            call_command(self.command)

        mock_track_event.assert_not_called()
        self.assertIn('[Monthly Impact Report] Execution completed.', '\n'.join(log.output))

    @mock.patch(
        'enterprise_access.apps.core.management.commands.monthly_impact_report.track_event'
    )
    @mock.patch(
        'enterprise_access.apps.core.management.commands.monthly_impact_report.'
        'Command.get_query_results_from_reporting_db'
    )
    def test_handle_message_data_mapped_correctly(self, mock_get_rows, mock_track_event):
        """
        Verify that handle() maps row indices to the correct message_data keys.
        """
        mock_get_rows.return_value = [MOCK_ROW]

        with self.assertLogs(LOGGER_NAME, level='INFO'):
            call_command(self.command)

        mock_track_event.assert_called_once()
        _, _, kwargs = mock_track_event.call_args[0]

        self.assertEqual(kwargs['YEAR_MONTH'], MOCK_ROW[0])
        self.assertEqual(kwargs['ADMIN_LINK'], MOCK_ROW[2])
        self.assertEqual(kwargs['ENTERPRISE_NAME'], MOCK_ROW[3])
        self.assertEqual(kwargs['EXTERNAL_ID'], MOCK_ROW[5])
        self.assertEqual(kwargs['LEARNING_HRS'], MOCK_ROW[6])
        self.assertEqual(kwargs['NEW_ENROLLS'], MOCK_ROW[7])
        self.assertEqual(kwargs['NEW_COMPLETES'], MOCK_ROW[8])
        self.assertEqual(kwargs['SESSIONS'], MOCK_ROW[9])
        self.assertEqual(kwargs['TOP_5_SKILLS'], MOCK_ROW[10])
        self.assertEqual(kwargs['AVG_MINUTES_PER_LEARNER'], MOCK_ROW[11])
        self.assertEqual(kwargs['PERC_WITH_SESSIONS'], MOCK_ROW[12])
        self.assertEqual(kwargs['LEARNING_HOURS_DELTA_'], MOCK_ROW[14])
        self.assertEqual(kwargs['NEW_ENROLLS_DELTA_'], MOCK_ROW[15])
        self.assertEqual(kwargs['NEW_COMPLETES_DELTA_'], MOCK_ROW[16])
        self.assertEqual(kwargs['SESSIONS_DELTA_'], MOCK_ROW[17])
        self.assertEqual(kwargs['AVG_MINUTES_PER_LEARNER_DELTA_'], MOCK_ROW[18])
        self.assertEqual(kwargs['LEARNING_MINUTES_1'], MOCK_ROW[21])
        self.assertEqual(kwargs['NUM_COMPLETIONS_1'], MOCK_ROW[22])
        self.assertEqual(kwargs['LEARNING_MINUTES_2'], MOCK_ROW[24])
        self.assertEqual(kwargs['NUM_COMPLETIONS_2'], MOCK_ROW[25])
        self.assertEqual(kwargs['LEARNING_MINUTES_10'], MOCK_ROW[48])
        self.assertEqual(kwargs['NUM_COMPLETIONS_10'], MOCK_ROW[49])
