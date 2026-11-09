"""Django-side constants for Personalized Search (the portable core's constants live in ``core/constants.py``)."""


class SegmentEvents:
    """
    Server-side Segment event names (``edx.server.enterprise-access.<domain>.<action>``, as in other apps).
    The core names events briefly (``turn.completed``); this adds the prefix.
    """

    PREFIX = 'edx.server.enterprise-access.personalized-search'
    TURN_COMPLETED = f'{PREFIX}.turn.completed'
    TURN_FAILED = f'{PREFIX}.turn.failed'
    TURN_STOPPED = f'{PREFIX}.turn.stopped'

    @classmethod
    def name(cls, event: str) -> str:
        return f'{cls.PREFIX}.{event}'
