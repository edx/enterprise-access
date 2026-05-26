from unittest import mock

from enterprise_access.apps.track.management.commands import nudge_dormant_enrolled_enterprise_learners

# Mock Snowflake row representing a single dormant learner record with all 14 fields
# from the query. This should be updated if the query structure changes.
MOCK_ROW = (
    '1',                        # external_id
    'org',                      # org_name
    'Course',                   # course_title
    10,                         # enrollment_count
    1,                          # min_effort
    2,                          # max_effort
    3,                          # weeks_to_complete
    'Self Paced',               # pacing_type
    'subtitle',                 # pacing_subtitle
    'First Video',              # display_name
    'https://img',              # course_image
    'https://video',            # lms_web_url
    'https://discussion',       # discussion_link
    'https://home',             # home_link
)


@mock.patch(
    (
        'enterprise_access.apps.track.management.commands.'
        'nudge_dormant_enrolled_enterprise_learners.fetch_all_query_results'
    )
)
def test_get_query_results_from_snowflake_uses_shared_helper(mock_fetch_all_query_results):
    mock_fetch_all_query_results.return_value = [MOCK_ROW]

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
        'ENROLLMENT_COUNT': 10,
    }

    with mock.patch.object(
        nudge_dormant_enrolled_enterprise_learners,
        'LOGGER',
    ) as mock_logger:
        command.emit_event(**event_data)

    mock_track_event.assert_called_once_with(
        '1',
        'edx.bi.enterprise.user.dormant.nudge',
        event_data,
    )
    logged_message = mock_logger.info.call_args[0][0]
    assert 'LMS User Id' not in logged_message
    assert 'Organization Name' not in logged_message
    assert 'Course Title' not in logged_message
    assert 'external_id_hash=%s' in logged_message


@mock.patch('enterprise_access.apps.track.management.commands.nudge_dormant_enrolled_enterprise_learners.track_event')
def test_emit_event_converts_integer_external_id_to_string(mock_track_event):
    """Verify that integer EXTERNAL_ID (from Snowflake) is converted to string for track_event."""
    command = nudge_dormant_enrolled_enterprise_learners.Command()
    event_data = {
        'EXTERNAL_ID': 12345,  # Integer from Snowflake
        'ORG_NAME': 'org',
        'COURSE_TITLE': 'Course',
        'ENROLLMENT_COUNT': 10,
    }

    with mock.patch.object(
        nudge_dormant_enrolled_enterprise_learners,
        'LOGGER',
    ):
        command.emit_event(**event_data)

    # Verify track_event received the string version, not the integer
    mock_track_event.assert_called_once()
    call_args = mock_track_event.call_args
    assert call_args[0][0] == '12345'  # First arg should be string
    assert isinstance(call_args[0][0], str)


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
    mock_get_query_results.return_value = [MOCK_ROW]

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
    mock_get_query_results.return_value = [MOCK_ROW]

    command = nudge_dormant_enrolled_enterprise_learners.Command()
    command.handle(no_commit=True)

    mock_emit_event.assert_not_called()
