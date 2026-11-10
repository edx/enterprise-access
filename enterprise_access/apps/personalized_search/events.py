"""
Usage events sent from the Personalized Search views (tech spec KD-12): join keys and raw facts only.
Turn events come from the orchestrator (``core/telemetry.py``); these cover history and ratings.
"""
from django.conf import settings

from enterprise_access.apps.personalized_search.adapters.segment_sink import segment_emit

SESSION_OPENED = 'history.session-opened'
SESSION_RESUMED = 'history.session-resumed'
TURN_RATED = 'turn.rated'


def send(lms_user_id, event: str, *, conversation_id, enterprise_customer_uuid, **properties) -> None:
    """Send one event with the join keys and the cohort and engine tags. Never raises (``track_event``)."""
    segment_emit(lms_user_id, event, {
        'conversation_id': str(conversation_id), 'enterprise_customer_uuid': str(enterprise_customer_uuid),
        'cohort': settings.PERSONALIZED_SEARCH_ANALYTICS_COHORT, 'engine': settings.PERSONALIZED_SEARCH_ENGINE_LABEL,
        **properties,
    })
