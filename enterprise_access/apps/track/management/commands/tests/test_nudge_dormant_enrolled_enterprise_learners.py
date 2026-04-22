from unittest import mock

from enterprise_access.apps.track.management.commands import nudge_dormant_enrolled_enterprise_learners


@mock.patch(
    (
        'enterprise_access.apps.track.management.commands.'
        'nudge_dormant_enrolled_enterprise_learners.fetch_all_query_results'
    )
)
def test_get_query_results_from_snowflake_uses_shared_helper(mock_fetch_all_query_results):
    mock_fetch_all_query_results.return_value = [
        (
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
        ),
    ]

    command = nudge_dormant_enrolled_enterprise_learners.Command()
    rows = list(command.get_query_results_from_snowflake())

    mock_fetch_all_query_results.assert_called_once_with(
        nudge_dormant_enrolled_enterprise_learners.QUERY
    )
    assert rows == mock_fetch_all_query_results.return_value


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


def test_add_arguments_parses_no_commit_flag():
    command = nudge_dormant_enrolled_enterprise_learners.Command()
    parser = command.create_parser(
        'manage.py',
        'nudge_dormant_enrolled_enterprise_learners',
    )
    options = parser.parse_args(['--no-commit'])

    assert options.no_commit is True


@mock.patch.object(
    nudge_dormant_enrolled_enterprise_learners.Command,
    'emit_event',
)
@mock.patch.object(
    nudge_dormant_enrolled_enterprise_learners.Command,
    'get_query_results_from_snowflake',
)
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


@mock.patch.object(
    nudge_dormant_enrolled_enterprise_learners.Command,
    'emit_event',
)
@mock.patch.object(
    nudge_dormant_enrolled_enterprise_learners.Command,
    'get_query_results_from_snowflake',
)
def test_handle_dry_run_does_not_emit_events(mock_get_query_results, mock_emit_event):
    mock_get_query_results.return_value = [
        (
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
        ),
    ]

    command = nudge_dormant_enrolled_enterprise_learners.Command()
    command.handle(no_commit=True)

    mock_emit_event.assert_not_called()
