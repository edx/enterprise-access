"""
Wrapper methods for emitting events to Segment.

"""

import logging
from functools import lru_cache

import analytics
from analytics.request import APIError
from django.conf import settings
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeoutError

logger = logging.getLogger(__name__)


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


SEGMENT_DELIVERY_ERRORS = (APIError, RequestsConnectionError, RequestsTimeoutError)
"""What ``track_event_or_raise`` raises when Segment did not accept the event; a later attempt may succeed."""


@lru_cache(maxsize=1)
def _sync_client(write_key):
    """
    A Segment client that posts each event in the calling thread. The module-level client only queues events for a
    background thread, which can drop them (Segment unreachable for longer than its own retries, or the process
    exiting first) without the caller knowing.
    """
    return analytics.Client(write_key, sync_mode=True)


def track_event_or_raise(lms_user_id, event_name, properties, message_id):
    """
    Send a tracking event to Segment now, and raise one of ``SEGMENT_DELIVERY_ERRORS`` if Segment did not accept it.
    For callers that must know the event was delivered, e.g. a Celery task that retries until it is.

    Args:
        lms_user_id (str): LMS User ID of the user we want tracked with this event for cross-platform tracking.
        event_name (str): Name of the event.
        properties (dict): All the properties of an event.
        message_id (str): Deterministic Segment ``messageId``. Segment drops a messageId it has already seen
            recently, so a retried event is not counted twice.

    Returns:
        bool: True if Segment accepted the event, False if it was not sent because SEGMENT_KEY is not set.
    """
    if not getattr(settings, 'SEGMENT_KEY', None):
        logger.warning("Event %s for user_id %s not tracked because SEGMENT_KEY not set", event_name, lms_user_id)
        return False
    _sync_client(settings.SEGMENT_KEY).track(
        user_id=lms_user_id, event=event_name, properties=properties, message_id=message_id,
    )
    return True
