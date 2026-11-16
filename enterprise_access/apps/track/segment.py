"""
Wrapper methods for emitting events to Segment.

"""

import logging

import analytics
from django.conf import settings

logger = logging.getLogger(__name__)


def track_event(lms_user_id, event_name, properties, message_id=None):
    """
    Send a tracking event to segment

    Args:
        lms_user_id (str): LMS User ID of the user we want tracked with this event for cross-platform tracking.
        event_name (str): Name of the event.
        properties (dict): All the properties of an event.
        message_id (str): Optional deterministic Segment ``messageId``. Segment drops a messageId it has already
            seen recently, so a resent event is not counted twice. Defaults to a random one.

    Returns:
        None
    """
    if hasattr(settings, "SEGMENT_KEY") and settings.SEGMENT_KEY:
        try:  # We should never raise an exception when not able to send a tracking event.
            # message_id is passed only when given, so existing callers' calls are unchanged.
            extra_kwargs = {'message_id': message_id} if message_id else {}
            analytics.track(user_id=lms_user_id, event=event_name, properties=properties, **extra_kwargs)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception(exc)
    else:
        logger.warning(
            "Event %s for user_id %s not tracked because SEGMENT_KEY not set", event_name, lms_user_id
        )
