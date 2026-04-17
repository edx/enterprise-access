from unittest import mock

from django.test import override_settings

from enterprise_access.apps.track.management.commands import nudge_dormant_enrolled_enterprise_learners


@override_settings(
    SNOWFLAKE_SERVICE_USER='user',
    SNOWFLAKE_SERVICE_USER_PASSWORD='password',
    SNOWFLAKE_ACCOUNT='test_account',
    SNOWFLAKE_DATABASE='test_database',
)
@mock.patch('enterprise_access.apps.track.management.commands.nudge_dormant_enrolled_enterprise_learners.snowflake.connector.connect')
def test_get_query_results_from_snowflake_closes_cursor_and_connection(mock_connect):
    mock_cursor = mock.MagicMock()
    mock_cursor.fetchall.return_value = [('1', 'org', 'Course', 10, 1, 2, 3, 'Self Paced', 'subtitle', 'First Video', 'https://img', 'https://video', 'https://discussion', 'https://home')]
    mock_connection = mock.MagicMock()
    mock_connection.cursor.return_value = mock_cursor
    mock_connect.return_value = mock_connection

    command = nudge_dormant_enrolled_enterprise_learners.Command()
    rows = list(command.get_query_results_from_snowflake())

    mock_connect.assert_called_once_with(
        user='user',
        password='password',
        account='test_account',
        database='test_database',
    )
    mock_cursor.execute.assert_called_once_with(nudge_dormant_enrolled_enterprise_learners.QUERY)
    mock_cursor.fetchall.assert_called_once()
    mock_cursor.close.assert_called_once()
    mock_connection.close.assert_called_once()
    assert rows == mock_cursor.fetchall.return_value


@mock.patch('enterprise_access.apps.track.management.commands.nudge_dormant_enrolled_enterprise_learners.track_event')
def test_emit_event_calls_track_event(mock_track_event):
    command = nudge_dormant_enrolled_enterprise_learners.Command()
    event_data = {
        'EXTERNAL_ID': '1',
        'ORG_NAME': 'org',
        'COURSE_TITLE': 'Course',
    }

    command.emit_event(**event_data)

    mock_track_event.assert_called_once_with(
        '1',
        'edx.bi.enterprise.user.dormant.nudge',
        event_data,
    )


@mock.patch('enterprise_access.apps.track.management.commands.nudge_dormant_enrolled_enterprise_learners.Command.emit_event')
@mock.patch('enterprise_access.apps.track.management.commands.nudge_dormant_enrolled_enterprise_learners.Command.get_query_results_from_snowflake')
def test_handle_commits_and_emits_events(mock_get_query_results, mock_emit_event):
    row = (
        '1',
        'org',
        'Course',
        10,
        1,
        2,
        3,
        'Self Paced',
        'subtitle',
        'First Video',
        'https://img',
        'https://video',
        'https://discussion',
        'https://home',
    )
    mock_get_query_results.return_value = [row]

    command = nudge_dormant_enrolled_enterprise_learners.Command()
    command.handle(no_commit=False)

    mock_emit_event.assert_called_once_with(
        EXTERNAL_ID='1',
        ORG_NAME='org',
        COURSE_TITLE='Course',
        ENROLLMENT_COUNT=10,
        MIN_EFFORT=1,
        MAX_EFFORT=2,
        WEEKS_TO_COMPLETE=3,
        PACING_TYPE='Self Paced',
        PACING_SUBTITLE='subtitle',
        DISPLAY_NAME='First Video',
        COURSE_IMAGE='https://img',
        LMS_WEB_URL='https://video',
        DISCUSSION_LINK='https://discussion',
        HOME_LINK='https://home',
    )


@mock.patch('enterprise_access.apps.track.management.commands.nudge_dormant_enrolled_enterprise_learners.Command.emit_event')
@mock.patch('enterprise_access.apps.track.management.commands.nudge_dormant_enrolled_enterprise_learners.Command.get_query_results_from_snowflake')
def test_handle_dry_run_does_not_emit_events(mock_get_query_results, mock_emit_event):
    mock_get_query_results.return_value = [
        ('1', 'org', 'Course', 10, 1, 2, 3, 'Self Paced', 'subtitle', 'First Video', 'https://img', 'https://video', 'https://discussion', 'https://home')
    ]

    command = nudge_dormant_enrolled_enterprise_learners.Command()
    command.handle(no_commit=True)

    mock_emit_event.assert_not_called()
