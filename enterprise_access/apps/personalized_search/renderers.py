"""DRF renderer so content negotiation accepts the portal's ``Accept: text/event-stream``."""
import json

from rest_framework.renderers import BaseRenderer


class EventStreamRenderer(BaseRenderer):
    """
    Lets DRF content negotiation accept ``Accept: text/event-stream`` (sent by the portal's SSE
    transport). The stream itself is a StreamingHttpResponse; this only renders the JSON error
    bodies (400/401/403/404/409/503) that DRF returns before streaming starts.
    """

    media_type = 'text/event-stream'
    format = 'event-stream'
    charset = 'utf-8'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return json.dumps(data).encode(self.charset)
