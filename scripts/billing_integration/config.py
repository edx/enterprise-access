"""
Configuration management for billing integration tests.

Loads settings from environment variables with validation.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:
    raise Exception("dotenv package unavailable, please install scripts/requirements.txt")


@dataclass
class Config:
    """
    Configuration loaded from environment variables.

    All sensitive credentials should be stored in environment variables
    or a .env file, never committed to version control.
    """
    # OAuth JWT credentials
    oauth_client_id: str
    oauth_client_secret: str
    oauth_token_url: str

    # API settings
    api_base_url: str
    enterprise_customer_uuid: str

    # Optional Stripe credentials (for verification/debugging)
    stripe_api_key: Optional[str] = None
    stripe_customer_id: Optional[str] = None

    # Optional test user credentials
    test_user_email: Optional[str] = None
    test_user_password: Optional[str] = None

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None) -> 'Config':
        """
        Load configuration from environment variables.

        Args:
            env_file: Optional path to .env file. If not provided, will look for
                     .env in the current directory.

        Returns:
            Config instance with loaded settings

        Raises:
            ValueError: If required environment variables are missing
            FileNotFoundError: If specified env_file doesn't exist
        """
        # Load from .env file if provided or if default exists
        if env_file:
            if not env_file.exists():
                raise FileNotFoundError(f"Environment file not found: {env_file}")
            load_dotenv(env_file)
        elif Path('.env').exists():
            load_dotenv('.env')

        # Define required environment variables
        required_vars = [
            'OAUTH_CLIENT_ID',
            'OAUTH_CLIENT_SECRET',
            'OAUTH_TOKEN_URL',
            'API_BASE_URL',
            'ENTERPRISE_CUSTOMER_UUID',
        ]

        # Check for missing required variables
        missing = [var for var in required_vars if not os.getenv(var)]
        if missing:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing)}\n"
                f"Please set these in your environment or create a .env file.\n"
                f"See .env.example for reference."
            )

        return cls(
            oauth_client_id=os.getenv('OAUTH_CLIENT_ID'),
            oauth_client_secret=os.getenv('OAUTH_CLIENT_SECRET'),
            oauth_token_url=os.getenv('OAUTH_TOKEN_URL'),
            api_base_url=os.getenv('API_BASE_URL'),
            enterprise_customer_uuid=os.getenv('ENTERPRISE_CUSTOMER_UUID'),
            stripe_api_key=os.getenv('STRIPE_API_KEY'),
            stripe_customer_id=os.getenv('STRIPE_CUSTOMER_ID'),
            test_user_email=os.getenv('TEST_USER_EMAIL'),
            test_user_password=os.getenv('TEST_USER_PASSWORD'),
        )

    def validate(self) -> None:
        """
        Validate configuration values.

        Raises:
            ValueError: If configuration values are invalid
        """
        # Validate URLs
        if not self.api_base_url.startswith(('http://', 'https://')):
            raise ValueError(f"Invalid API_BASE_URL: {self.api_base_url}")

        if not self.oauth_token_url.startswith(('http://', 'https://')):
            raise ValueError(f"Invalid OAUTH_TOKEN_URL: {self.oauth_token_url}")

        # Validate UUID format (basic check)
        if len(self.enterprise_customer_uuid) < 32:
            raise ValueError(
                f"Invalid ENTERPRISE_CUSTOMER_UUID: {self.enterprise_customer_uuid}"
            )

    def mask_secrets(self) -> dict:
        """
        Return a dictionary representation with secrets masked.

        Useful for logging configuration without exposing credentials.
        """
        return {
            'oauth_client_id': self.oauth_client_id[:8] + '...' if self.oauth_client_id else None,
            'oauth_client_secret': '***' if self.oauth_client_secret else None,
            'oauth_token_url': self.oauth_token_url,
            'api_base_url': self.api_base_url,
            'enterprise_customer_uuid': self.enterprise_customer_uuid,
            'stripe_api_key': '***' if self.stripe_api_key else None,
            'stripe_customer_id': self.stripe_customer_id[:8] + '...' if self.stripe_customer_id else None,
            'test_user_email': self.test_user_email,
            'test_user_password': '***' if self.test_user_password else None,
        }
