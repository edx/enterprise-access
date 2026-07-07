"""
Tests for provisioning serializers.
"""
from uuid import uuid4

import ddt
from django.test import TestCase

from enterprise_access.apps.api.serializers.provisioning import (
    ProvisioningRequestSerializer,
    ProvisioningResponseSerializer
)


@ddt.ddt
class TestProvisioningSerializers(TestCase):
    """
    Tests for academy provisioning serializer validation.
    """

    def _build_request_payload(self, *, include_academy):
        """
        Build a request payload for the ProvisioningRequestSerializer."""
        payload = {
            'enterprise_customer': {
                'name': 'Test Customer',
                'slug': 'test-customer',
                'country': 'US',
            },
            'pending_admins': [],
            'enterprise_catalog': {
                'title': 'Test Catalog',
                'catalog_query_id': 2,
            },
            'academy': {
                'academy_uuid': str(uuid4()),
            },
            'customer_agreement': {},
            'trial_subscription_plan': {
                'title': 'Trial Plan',
                'salesforce_opportunity_line_item': 'oli-1',
                'start_date': '2025-06-01T00:00:00Z',
                'expiration_date': '2026-06-01T00:00:00Z',
                'ssp_product_slug': 'ai-academy-trial',
                'product_id': 1,
                'desired_num_licenses': 5,
            },
            'first_paid_subscription_plan': {
                'title': 'Paid Plan',
                'salesforce_opportunity_line_item': None,
                'start_date': '2026-06-01T00:00:00Z',
                'expiration_date': '2027-06-01T00:00:00Z',
                'ssp_product_slug': 'ai-academy-paid',
                'product_id': 2,
                'desired_num_licenses': 5,
            },
        }
        if include_academy:
            payload['academy'] = {
                'academy_uuid': str(uuid4()),
            }
        return payload

    @ddt.data(
        {'scenario': 'with academy', 'include_academy': True},
        {'scenario': 'without academy', 'include_academy': False},
    )
    @ddt.unpack
    def test_request_serializer_accepts_optional_academy(self, scenario, include_academy):
        serializer = ProvisioningRequestSerializer(data=self._build_request_payload(include_academy=include_academy))

        self.assertTrue(serializer.is_valid(), msg=f'{scenario}: {serializer.errors}')

    def _build_response_payload(self):
        return {
            'enterprise_customer': {
                'uuid': str(uuid4()),
                'name': 'Test Customer',
                'country': 'US',
                'slug': 'test-customer',
            },
            'customer_admins': {
                'created_admins': [],
                'existing_admins': [],
            },
            'enterprise_catalog': {
                'uuid': str(uuid4()),
                'enterprise_customer_uuid': str(uuid4()),
                'title': 'Test Catalog',
                'catalog_query_id': 2,
            },
            'academy': {
                'academy_uuid': None,
                'enterprise_catalog_uuid': str(uuid4()),
            },
            'customer_agreement': {
                'uuid': str(uuid4()),
                'enterprise_customer_uuid': str(uuid4()),
                'default_catalog_uuid': str(uuid4()),
                'subscriptions': [],
            },
            'trial_subscription_plan': {
                'uuid': str(uuid4()),
                'title': 'Trial Plan',
                'salesforce_opportunity_line_item': 'oli-1',
                'created': '2025-06-01T00:00:00Z',
                'start_date': '2025-06-01T00:00:00Z',
                'expiration_date': '2026-06-01T00:00:00Z',
                'is_active': True,
                'is_current': True,
                'plan_type': 'trial',
                'enterprise_catalog_uuid': str(uuid4()),
                'product': 1,
            },
            'first_paid_subscription_plan': {
                'uuid': str(uuid4()),
                'title': 'Paid Plan',
                'salesforce_opportunity_line_item': 'oli-2',
                'created': '2026-06-01T00:00:00Z',
                'start_date': '2026-06-01T00:00:00Z',
                'expiration_date': '2027-06-01T00:00:00Z',
                'is_active': True,
                'is_current': True,
                'plan_type': 'paid',
                'enterprise_catalog_uuid': str(uuid4()),
                'product': 2,
            },
            'subscription_plan_renewal': {
                'id': 1,
                'prior_subscription_plan': str(uuid4()),
                'renewed_subscription_plan': str(uuid4()),
                'number_of_licenses': 5,
                'effective_date': '2026-06-01T00:00:00Z',
                'renewed_expiration_date': '2027-06-01T00:00:00Z',
                'salesforce_opportunity_line_item': 'oli-3',
            },
        }

    def test_response_serializer_accepts_academy_output(self):
        serializer = ProvisioningResponseSerializer(data=self._build_response_payload())

        self.assertTrue(serializer.is_valid(), serializer.errors)
