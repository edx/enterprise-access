"""
API client wrapper for Billing Management endpoints.

Provides a high-level interface for interacting with the billing management API.
"""
import logging
from typing import Any, Dict, Optional

import requests

from .auth import JWTAuthenticator


logger = logging.getLogger(__name__)


class BillingManagementClient:
    """
    Client for billing management API endpoints.

    This class wraps all billing management API operations with automatic
    JWT authentication and error handling.
    """

    def __init__(self, base_url: str, authenticator: JWTAuthenticator):
        """
        Initialize the billing management client.

        Args:
            base_url: Base URL for the API (e.g., http://localhost:18270/api/v1)
            authenticator: JWT authenticator instance for authentication
        """
        self.base_url = base_url.rstrip('/')
        self.auth = authenticator

    def _request(
        self,
        method: str,
        endpoint: str,
        log_response: bool = True,
        **kwargs
    ) -> requests.Response:
        """
        Make an authenticated HTTP request.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path (relative to base_url)
            log_response: Whether to log response details
            **kwargs: Additional arguments to pass to requests.request()

        Returns:
            Response object from requests library

        Raises:
            requests.HTTPError: If request fails
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self.auth.get_headers()

        # Merge any additional headers
        if 'headers' in kwargs:
            headers.update(kwargs.pop('headers'))

        logger.debug(f"{method} {url}")
        if 'params' in kwargs:
            logger.debug(f"Params: {kwargs['params']}")
        if 'json' in kwargs:
            logger.debug(f"Body: {kwargs['json']}")

        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                timeout=30,
                **kwargs
            )

            if log_response:
                logger.info(
                    f"{method} {endpoint} -> {response.status_code} "
                    f"({len(response.content)} bytes)"
                )

            return response

        except requests.RequestException as e:
            logger.error(f"Request failed: {e}")
            raise

    # Health check endpoint
    def health_check(self) -> Dict[str, Any]:
        """
        Check billing management API health.

        Returns:
            Health status response
        """
        response = self._request('GET', 'billing-management/health-check/')
        response.raise_for_status()
        return response.json()

    # Address endpoints
    def get_address(self, enterprise_uuid: str) -> Dict[str, Any]:
        """
        Get billing address for an enterprise.

        Args:
            enterprise_uuid: UUID of the enterprise customer

        Returns:
            Dictionary containing address fields (name, email, phone, address, etc.)
        """
        response = self._request(
            'GET',
            'billing-management/address/',
            params={'enterprise_customer_uuid': enterprise_uuid}
        )
        response.raise_for_status()
        return response.json()

    def update_address(
        self,
        enterprise_uuid: str,
        address_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Update billing address for an enterprise.

        Args:
            enterprise_uuid: UUID of the enterprise customer
            address_data: Dictionary with address fields:
                - name (str): Customer name
                - email (str): Email address
                - phone (str, optional): Phone number
                - country (str): Two-letter country code
                - address_line_1 (str): Street address
                - address_line_2 (str, optional): Additional address line
                - city (str): City name
                - state (str, optional): State/province code
                - postal_code (str): ZIP/postal code

        Returns:
            Updated address data
        """
        response = self._request(
            'POST',
            'billing-management/address/update/',
            params={'enterprise_customer_uuid': enterprise_uuid},
            json=address_data
        )
        response.raise_for_status()
        return response.json()

    # Payment method endpoints
    def list_payment_methods(self, enterprise_uuid: str) -> Dict[str, Any]:
        """
        List payment methods for an enterprise.

        Args:
            enterprise_uuid: UUID of the enterprise customer

        Returns:
            Dictionary with 'payment_methods' list containing payment method details
        """
        response = self._request(
            'GET',
            'billing-management/payment-methods/',
            params={'enterprise_customer_uuid': enterprise_uuid}
        )
        response.raise_for_status()
        return response.json()

    def set_default_payment_method(
        self,
        enterprise_uuid: str,
        payment_method_id: str
    ) -> Dict[str, Any]:
        """
        Set a payment method as default.

        Args:
            enterprise_uuid: UUID of the enterprise customer
            payment_method_id: Stripe payment method ID (e.g., pm_xxx)

        Returns:
            Success response
        """
        response = self._request(
            'POST',
            f'billing-management/payment-methods/{payment_method_id}/set-default/',
            params={'enterprise_customer_uuid': enterprise_uuid}
        )
        response.raise_for_status()
        return response.json()

    def delete_payment_method(
        self,
        enterprise_uuid: str,
        payment_method_id: str
    ) -> Dict[str, Any]:
        """
        Delete a payment method.

        Args:
            enterprise_uuid: UUID of the enterprise customer
            payment_method_id: Stripe payment method ID (e.g., pm_xxx)

        Returns:
            Success response
        """
        response = self._request(
            'DELETE',
            f'billing-management/payment-methods/{payment_method_id}/',
            params={'enterprise_customer_uuid': enterprise_uuid}
        )
        response.raise_for_status()
        return response.json()

    # Transaction endpoints
    def list_transactions(
        self,
        enterprise_uuid: str,
        limit: int = 10,
        page_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        List transactions (invoices) for an enterprise.

        Args:
            enterprise_uuid: UUID of the enterprise customer
            limit: Maximum number of transactions to return (1-25, default 10)
            page_token: Pagination token for next page

        Returns:
            Dictionary with 'transactions' list and optional 'next_page_token'
        """
        params = {
            'enterprise_customer_uuid': enterprise_uuid,
            'limit': limit,
        }
        if page_token:
            params['page_token'] = page_token

        response = self._request(
            'GET',
            'billing-management/transactions/',
            params=params
        )
        response.raise_for_status()
        return response.json()

    # Subscription endpoints
    def get_subscription(self, enterprise_uuid: str) -> Optional[Dict[str, Any]]:
        """
        Get subscription details for an enterprise.

        Args:
            enterprise_uuid: UUID of the enterprise customer

        Returns:
            Subscription details dictionary, or None if no active subscription
        """
        response = self._request(
            'GET',
            'billing-management/subscription/',
            params={'enterprise_customer_uuid': enterprise_uuid}
        )
        response.raise_for_status()
        data = response.json()
        # API returns null for no subscription
        return data.get('subscription')

    def cancel_subscription(self, enterprise_uuid: str) -> Dict[str, Any]:
        """
        Cancel a subscription at the end of the current billing period.

        Note: Only Teams and Essentials plans can be cancelled.

        Args:
            enterprise_uuid: UUID of the enterprise customer

        Returns:
            Updated subscription details with cancel_at_period_end=True
        """
        response = self._request(
            'POST',
            'billing-management/subscription/cancel/',
            params={'enterprise_customer_uuid': enterprise_uuid}
        )
        response.raise_for_status()
        return response.json()

    def reinstate_subscription(self, enterprise_uuid: str) -> Dict[str, Any]:
        """
        Reinstate a subscription that was scheduled for cancellation.

        Note: Only works if subscription is scheduled for cancellation
        and the current period has not ended yet.

        Args:
            enterprise_uuid: UUID of the enterprise customer

        Returns:
            Updated subscription details with cancel_at_period_end=False
        """
        response = self._request(
            'POST',
            'billing-management/subscription/reinstate/',
            params={'enterprise_customer_uuid': enterprise_uuid}
        )
        response.raise_for_status()
        return response.json()

    # Convenience methods for common workflows
    def get_all_transactions(self, enterprise_uuid: str) -> list:
        """
        Get all transactions by paginating through results.

        Args:
            enterprise_uuid: UUID of the enterprise customer

        Returns:
            List of all transaction dictionaries
        """
        all_transactions = []
        page_token = None

        while True:
            response = self.list_transactions(
                enterprise_uuid,
                limit=25,
                page_token=page_token
            )

            transactions = response.get('transactions', [])
            all_transactions.extend(transactions)

            page_token = response.get('next_page_token')
            if not page_token:
                break

        logger.info(f"Retrieved {len(all_transactions)} total transactions")
        return all_transactions

    def get_default_payment_method_id(self, enterprise_uuid: str) -> Optional[str]:
        """
        Get the ID of the default payment method.

        Args:
            enterprise_uuid: UUID of the enterprise customer

        Returns:
            Payment method ID or None if no default set
        """
        payment_methods = self.list_payment_methods(enterprise_uuid)
        methods = payment_methods.get('payment_methods', [])

        for method in methods:
            if method.get('is_default'):
                return method.get('id')

        return None
