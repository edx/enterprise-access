import json
from unittest import mock

import pytest
import requests
from analytics.request import APIError as SegmentAPIError
from django.test import override_settings

from enterprise_access.apps.track.segment import SYNC_SEND_TIMEOUT_SECONDS, send_event_synchronously, track_event

mock_lms_user_id = 'lms_user_id'
mock_event_name = 'mock.event.name'


@override_settings(SEGMENT_KEY=None)
@mock.patch('enterprise_access.apps.track.segment.logger', return_value=mock.MagicMock())
def test_track_event_no_segment_key(mock_logger):
    track_event(mock_lms_user_id, mock_event_name, {})
    mock_logger.warning.assert_called_with(
        "Event %s for user_id %s not tracked because SEGMENT_KEY not set", mock_event_name, mock_lms_user_id
    )


@override_settings(SEGMENT_KEY='123')
@mock.patch('enterprise_access.apps.track.segment.logger', return_value=mock.MagicMock())
@mock.patch('enterprise_access.apps.track.segment.analytics', return_value=mock.MagicMock())
def test_track_event_catches_exceptions(mock_analytics, mock_logger):
    mock_analytics.track.side_effect = Exception('Something went wrong')
    track_event(mock_lms_user_id, mock_event_name, {})
    mock_logger.exception.assert_called()


def _segment_response(status_code):
    response = mock.Mock(status_code=status_code)
    response.json.return_value = {'code': 'error_code', 'message': 'error message'}
    return response


@override_settings(SEGMENT_KEY=None)
@mock.patch('analytics.request._session')
def test_send_event_synchronously_without_key_sends_nothing(mock_session):
    assert send_event_synchronously(mock_lms_user_id, mock_event_name, {}, message_id='m1') is False
    mock_session.post.assert_not_called()


@override_settings(SEGMENT_KEY='123')
@mock.patch('analytics.request._session')
def test_send_event_synchronously_posts_once_with_timeout_and_message_id(mock_session):
    """The event is posted in the calling thread, with a bounded timeout and the caller's messageId."""
    mock_session.post.return_value = _segment_response(200)

    assert send_event_synchronously(mock_lms_user_id, mock_event_name, {'a': 1}, message_id='m1') is True

    mock_session.post.assert_called_once()
    call_kwargs = mock_session.post.call_args.kwargs
    assert call_kwargs['timeout'] == SYNC_SEND_TIMEOUT_SECONDS
    message = json.loads(call_kwargs['data'])['batch'][0]
    assert message['messageId'] == 'm1'
    assert message['event'] == mock_event_name
    assert message['userId'] == mock_lms_user_id
    assert message['properties'] == {'a': 1}


@override_settings(SEGMENT_KEY='123')
@mock.patch('analytics.request._session')
def test_send_event_synchronously_raises_when_segment_rejects_the_event(mock_session):
    """Unlike track_event, an upload failure reaches the caller instead of being swallowed on a background thread."""
    mock_session.post.return_value = _segment_response(503)

    with pytest.raises(SegmentAPIError) as exc_info:
        send_event_synchronously(mock_lms_user_id, mock_event_name, {}, message_id='m1')

    assert exc_info.value.status == 503
    mock_session.post.assert_called_once()  # no retries inside the client; the caller decides


@override_settings(SEGMENT_KEY='123')
@mock.patch('analytics.request._session')
def test_send_event_synchronously_raises_on_network_error(mock_session):
    mock_session.post.side_effect = requests.Timeout('timed out')

    with pytest.raises(requests.Timeout):
        send_event_synchronously(mock_lms_user_id, mock_event_name, {}, message_id='m1')
