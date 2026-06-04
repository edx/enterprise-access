"""
OAuth JWT authentication for API requests.

Handles token acquisition, caching, and automatic refresh.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import requests


logger = logging.getLogger(__name__)


class JWTAuthenticator:
    """
    Handle OAuth JWT authentication with token caching and auto-refresh.

    This class manages the OAuth client credentials flow to obtain JWT
    access tokens for API authentication. It automatically refreshes
    tokens before they expire.
    """

    def __init__(self, client_id: str, client_secret: str, token_url: str):
        """
        Initialize the JWT authenticator.

        Args:
            client_id: OAuth application client ID
            client_secret: OAuth application client secret
            token_url: URL endpoint for token acquisition
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self._token: Optional[str] = None
        self._token_expires: Optional[datetime] = None

    def get_token(self, force_refresh: bool = False) -> str:
        """
        Get a valid JWT access token.

        Returns a cached token if available and not expired, otherwise
        requests a new token from the OAuth server.

        Args:
            force_refresh: If True, force a new token request even if
                          cached token is still valid

        Returns:
            Valid JWT access token string

        Raises:
            requests.HTTPError: If token request fails
            KeyError: If token response is missing expected fields
        """
        # Check if we have a valid cached token
        if not force_refresh and self._token and self._token_expires:
            if datetime.now() < self._token_expires:
                logger.debug("Using cached JWT token")
                return self._token

        # Request new token using client credentials flow
        logger.info(f"Requesting new JWT token from {self.token_url}")

        try:
            response = requests.post(
                self.token_url,
                data={
                    'grant_type': 'client_credentials',
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                    'token_type': 'jwt',
                },
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Failed to obtain JWT token: {e}")
            raise

        data = response.json()

        # Extract token and expiration
        try:
            self._token = data['access_token']
            expires_in = data.get('expires_in', 3600)  # Default 1 hour
            # Subtract 60 seconds as buffer to avoid using expired tokens
            self._token_expires = datetime.now() + timedelta(seconds=expires_in - 60)

            logger.info(
                f"Successfully obtained JWT token (expires in {expires_in}s, "
                f"cached until {self._token_expires})"
            )

            return self._token

        except KeyError as e:
            logger.error(f"Token response missing expected field: {e}")
            logger.debug(f"Response data: {data}")
            raise

    def get_headers(self) -> dict:
        """
        Get HTTP headers with authorization token.

        Returns:
            Dictionary of HTTP headers including Authorization header
            with Bearer token and Content-Type set to application/json
        """
        return {
            'Authorization': f'JWT {self.get_token()}',
            'Content-Type': 'application/json',
        }

    def clear_token(self) -> None:
        """
        Clear cached token.

        Useful for forcing a refresh on the next request or for
        cleanup/testing purposes.
        """
        logger.debug("Clearing cached JWT token")
        self._token = None
        self._token_expires = None

    @property
    def is_token_valid(self) -> bool:
        """
        Check if the cached token is still valid.

        Returns:
            True if cached token exists and hasn't expired, False otherwise
        """
        if not self._token or not self._token_expires:
            return False
        return datetime.now() < self._token_expires

    @property
    def token_expires_in(self) -> Optional[int]:
        """
        Get seconds until token expiration.

        Returns:
            Number of seconds until token expires, or None if no token cached
        """
        if not self._token_expires:
            return None

        delta = self._token_expires - datetime.now()
        return max(0, int(delta.total_seconds()))
