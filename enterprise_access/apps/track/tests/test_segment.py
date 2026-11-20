import json
from unittest import mock

import pytest
from django.test import override_settings
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeoutError

from enterprise_access.apps.track.segment import (
    SEGMENT_DELIVERY_ERRORS,
    _sync_client,
    track_event,
    track_event_or_raise
)

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


@pytest.fixture(name='sync_client_cache')
def fixture_sync_client_cache():
    _sync_client.cache_clear()
    yield
    _sync_client.cache_clear()


@pytest.mark.usefixtures('sync_client_cache')
@override_settings(SEGMENT_KEY='123')
@mock.patch('enterprise_access.apps.track.segment.analytics.request._session')
def test_track_event_or_raise_posts_in_the_calling_thread(mock_session):
    """The event is posted before the call returns, with the given messageId, not left on a queue."""
    mock_session.post.return_value = mock.Mock(status_code=200)

    assert track_event_or_raise(mock_lms_user_id, mock_event_name, {'a': 1}, message_id='event:order') is True

    mock_session.post.assert_called_once()
    batch = json.loads(mock_session.post.call_args.kwargs['data'])['batch']
    assert [(m['userId'], m['event'], m['properties'], m['messageId']) for m in batch] == [
        (mock_lms_user_id, mock_event_name, {'a': 1}, 'event:order'),
    ]


@pytest.mark.parametrize('failure', [
    mock.Mock(status_code=503, json=mock.Mock(return_value={'code': 'unavailable', 'message': 'down'})),
    RequestsConnectionError('unreachable'),
    RequestsTimeoutError('timed out'),
])
@pytest.mark.usefixtures('sync_client_cache')
@override_settings(SEGMENT_KEY='123')
@mock.patch('enterprise_access.apps.track.segment.analytics.request._session')
def test_track_event_or_raise_raises_when_not_delivered(mock_session, failure):
    if isinstance(failure, Exception):
        mock_session.post.side_effect = failure
    else:
        mock_session.post.return_value = failure

    with pytest.raises(SEGMENT_DELIVERY_ERRORS):
        track_event_or_raise(mock_lms_user_id, mock_event_name, {}, message_id='event:order')


@override_settings(SEGMENT_KEY=None)
@mock.patch('enterprise_access.apps.track.segment.analytics.request._session')
def test_track_event_or_raise_without_segment_key_sends_nothing(mock_session):
    assert track_event_or_raise(mock_lms_user_id, mock_event_name, {}, message_id='event:order') is False
    mock_session.post.assert_not_called()
