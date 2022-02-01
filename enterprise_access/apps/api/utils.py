"""
Utility functions for Enterprise Access API.
"""

from uuid import UUID

from rest_framework.exceptions import ParseError


def get_enterprise_uuid_from_query_params(request):
    """
    Extracts enterprise_customer_uuid from query params.
    """

    enterprise_customer_uuid = request.query_params.get('enterprise_customer_uuid')

    if not enterprise_customer_uuid:
        return None

    try:
        return UUID(enterprise_customer_uuid)
    except ValueError as ex:
        raise ParseError('{} is not a valid uuid.'.format(enterprise_customer_uuid)) from ex

def get_enterprise_uuid_from_request_data(request):
    """
    Extracts enterprise_customer_uuid from the request payload.
    """

    enterprise_customer_uuid = request.data.get('enterprise_customer_uuid')

    if not enterprise_customer_uuid:
        return None

    try:
        return UUID(enterprise_customer_uuid)
    except ValueError as ex:
        raise ParseError('{} is not a valid uuid.'.format(enterprise_customer_uuid)) from ex
