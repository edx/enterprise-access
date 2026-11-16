"""
Wrapper methods for emitting events to Segment.

"""

import logging

import analytics
from django.conf import settings

logger = logging.getLogger(__name__)

# Seconds to wait for Segment to accept a synchronously sent event. Far below CELERY_TASK_SOFT_TIME_LIMIT, so a
# Segment outage fails the attempt (and the caller retries) instead of the worker killing the task mid-send.
SYNC_SEND_TIMEOUT_SECONDS = 10


def track_event(lms_user_id, event_name, properties):
    """
    Send a tracking event to segment

    Args:
        lms_user_id (str): LMS User ID of the user we want tracked with this event for cross-platform tracking.
        event_name (str): Name of the event.
        properties (dict): All the properties of an event.

    Returns:
        None
    """
    if hasattr(settings, "SEGMENT_KEY") and settings.SEGMENT_KEY:
        try:  # We should never raise an exception when not able to send a tracking event.
            analytics.track(user_id=lms_user_id, event=event_name, properties=properties)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception(exc)
    else:
        logger.warning(
            "Event %s for user_id %s not tracked because SEGMENT_KEY not set", event_name, lms_user_id
        )


def send_event_synchronously(lms_user_id, event_name, properties, message_id):
    """
    Send a tracking event to Segment and wait until Segment has accepted it.

    ``track_event`` hands events to analytics-python's background thread, which retries for minutes and then
    drops a failed upload without telling the caller. This instead posts the event once, in the calling thread,
    with a bounded timeout, and raises if Segment does not accept it. Use it when the caller records that the
    event was sent and must never record a send that failed.

    Args:
        lms_user_id (str): LMS User ID of the user we want tracked with this event for cross-platform tracking.
        event_name (str): Name of the event.
        properties (dict): All the properties of an event.
        message_id (str): Deterministic Segment ``messageId``. Segment drops a messageId it has already seen in
            the last 24 hours, so resending after a success that the caller failed to record is not counted twice.

    Returns:
        bool: True once Segment accepted the event, False (nothing sent) if SEGMENT_KEY is not set.

    Raises:
        analytics.request.APIError: Segment responded with a non-200 status.
        requests.RequestException: The request failed or timed out.
        AssertionError: An argument has the wrong type (analytics-python's own checks).
    """
    if not getattr(settings, 'SEGMENT_KEY', None):
        logger.warning(
            "Event %s for user_id %s not sent because SEGMENT_KEY not set", event_name, lms_user_id
        )
        return False
    client = analytics.Client(write_key=settings.SEGMENT_KEY, sync_mode=True, timeout=SYNC_SEND_TIMEOUT_SECONDS)
    client.track(user_id=lms_user_id, event=event_name, properties=properties, message_id=message_id)
    return True
