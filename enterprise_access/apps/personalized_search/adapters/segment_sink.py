"""``Steps.emit`` over enterprise-access's Segment helper (tech spec KD-12)."""
from enterprise_access.apps.personalized_search.constants import SegmentEvents
from enterprise_access.apps.track.segment import track_event


def segment_emit(lms_user_id, event: str, properties: dict) -> None:
    """Send one usage event; ``track_event`` already logs and swallows failures."""
    track_event(lms_user_id=lms_user_id, event_name=SegmentEvents.name(event), properties=properties)
