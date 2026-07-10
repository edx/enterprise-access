"""
base API client
"""
import logging

from django.conf import settings
from edx_rest_api_client.client import OAuthAPIClient

logger = logging.getLogger(__name__)


class BaseOAuthClient:
    """
    API client for calls to the other services.
    """

    def __init__(self):
        self.client = OAuthAPIClient(
            settings.SOCIAL_AUTH_EDX_OAUTH2_URL_ROOT.strip('/'),
            self.oauth2_client_id,
            self.oauth2_client_secret
        )

    @property
    def oauth2_client_id(self):
        return settings.BACKEND_SERVICE_EDX_OAUTH2_KEY

    @property
    def oauth2_client_secret(self):
        return settings.BACKEND_SERVICE_EDX_OAUTH2_SECRET

    def get_paginated_results(self, start_url, params, timeout, traverse_pagination=False):
        """
        GET a paginated DRF endpoint and return a flat list of all results.

        Raises HTTPError on any non-2xx response; callers are responsible for
        logging and re-raising if needed.
        """
        results = []
        next_url = start_url
        while next_url:
            response = self.client.get(next_url, params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            results.extend(data.get('results', []))
            next_url = data.get('next') if traverse_pagination else None
        return results
