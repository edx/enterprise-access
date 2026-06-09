"""Tests for SSP Essentials products API endpoint."""
# pylint: disable=protected-access
import uuid
from decimal import Decimal
from unittest import mock

import ddt
import pytest
import stripe
from django.http import Http404
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from enterprise_access.apps.api.serializers.customer_billing import SspEssentialsProductResponseSerializer
from enterprise_access.apps.api.v1.views.customer_billing import BillingManagementViewSet, SspProductViewSet
from enterprise_access.apps.core.constants import SYSTEM_ENTERPRISE_LEARNER_ROLE
from enterprise_access.apps.customer_billing.models import SspProduct
from enterprise_access.apps.customer_billing.pricing_api import StripePricingError
from test_utils import APITest


@ddt.ddt
class CustomerBillingSspProductsTests(APITest):
    """Tests for public ssp-products viewset endpoints."""

    def setUp(self):
        super().setUp()
        self.set_jwt_cookie([{
            'system_wide_role': SYSTEM_ENTERPRISE_LEARNER_ROLE,
            'context': str(uuid.uuid4()),
        }])
        self.list_url = reverse('api:v1:ssp-products-list')
        # some tests mutate this object.
        self.essentials_product = SspProduct.objects.create(
            slug='ai-academy-yearly',
            stripe_price_lookup_key='ai_academy_yearly_price',
            academy_uuid=uuid.uuid4(),
            catalog_query_uuid=uuid.uuid4(),
            license_manager_product_id_trial=2,
            license_manager_product_id_paid=1,
            is_active=True,
        )

    @classmethod
    def setUpTestData(cls):
        """Create class-level fixtures that are not mutated by tests."""
        # `teams_product` is not modified by tests so create it once per TestCase
        SspProduct.objects.get_or_create(
            slug='teams-yearly',
            defaults={
                'stripe_price_lookup_key': 'teams_subscription_license_yearly',
                'academy_uuid': None,
                'catalog_query_uuid': uuid.uuid4(),
                'license_manager_product_id_trial': 2,
                'license_manager_product_id_paid': 1,
                'is_active': True,
            },
        )

    @staticmethod
    def _mock_price(lookup_key, unit_amount, product=None):
        """Construct a Stripe Price test object with optional expanded product payload."""
        price_payload = {
            'id': f'price_{lookup_key}',
            'lookup_key': lookup_key,
            'unit_amount': unit_amount,
            'currency': 'usd',
        }
        if product is not None:
            price_payload['product'] = product

        return stripe.Price.construct_from(
            price_payload,
            stripe.api_key,
        )

    @override_settings(SSP_ESSENTIALS_THUMBNAIL_S3_BASE_URL='https://s3.amazonaws.com/essentials-bucket')
    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.get_all_stripe_prices')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_list_ssp_products_success(self, mock_get_cached_academy_data, mock_get_all_stripe_prices):
        """List returns active academy-backed products and batch-fetches Stripe prices by lookup key."""
        academy_metadata = {
            'title': 'AI Academy',
            'long_name': 'AI Academy for Business',
            'description': 'Learn AI end-to-end',
            'marketing_url': 'https://example.com/ai',
            'thumbnail_url': 'academies/ai/thumbnail.png',
        }
        mock_get_cached_academy_data.side_effect = (
            lambda academy_uuid: academy_metadata if academy_uuid == self.essentials_product.academy_uuid else None
        )
        mock_get_all_stripe_prices.return_value = {
            'ai_academy_yearly_price': {
                'unit_amount_decimal': Decimal('149.00'),
                'stripe_name': 'AI Essentials',
            },
            'teams_subscription_license_yearly': {
                'unit_amount_decimal': Decimal('99.00'),
                'stripe_name': 'Teams',
            },
        }

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        essentials_payload = next(p for p in response.data if p['lookup_key'] == 'ai_academy_yearly_price')
        self.assertEqual(essentials_payload['name'], 'AI Academy')
        self.assertEqual(essentials_payload['long_name'], 'AI Academy for Business')
        self.assertEqual(essentials_payload['description'], 'Learn AI end-to-end')
        self.assertEqual(essentials_payload['marketing_url'], 'https://example.com/ai')
        self.assertEqual(
            essentials_payload['thumbnail_url'],
            'https://s3.amazonaws.com/essentials-bucket/academies/ai/thumbnail.png',
        )
        self.assertEqual(essentials_payload['price'], '149.00')

        mock_get_all_stripe_prices.assert_called_once()

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.get_all_stripe_prices')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_list_ssp_products_allows_anonymous_access(
        self,
        mock_get_cached_academy_data,
        mock_get_all_stripe_prices,
    ):
        """Endpoint does not require authentication."""
        self.client.logout()

        mock_get_cached_academy_data.return_value = {
            'title': 'AI Academy',
            'long_name': 'AI Academy for Business',
            'description': 'Learn AI end-to-end',
            'marketing_url': 'https://example.com/ai',
            'thumbnail_url': 'https://cdn.example.com/ai.png',
        }
        mock_get_all_stripe_prices.return_value = {
            'ai_academy_yearly_price': {'unit_amount_decimal': Decimal('149.00')}
        }

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(isinstance(response.data, list))

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.get_all_stripe_prices')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_list_ssp_products_keeps_absolute_thumbnail_url(
        self,
        mock_get_cached_academy_data,
        mock_get_all_stripe_prices,
    ):
        """Already absolute thumbnail URLs are not modified."""
        mock_get_cached_academy_data.return_value = {
            'title': 'AI Academy',
            'long_name': 'AI Academy for Business',
            'description': 'Learn AI end-to-end',
            'marketing_url': 'https://example.com/ai',
            'thumbnail_url': 'https://cdn.example.com/ai.png',
        }
        mock_get_all_stripe_prices.return_value = {
            'ai_academy_yearly_price': {'unit_amount_decimal': Decimal('149.00')}
        }

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = next(p for p in response.data if p['lookup_key'] == 'ai_academy_yearly_price')
        self.assertEqual(payload['thumbnail_url'], 'https://cdn.example.com/ai.png')

    @ddt.data(
        ({}, StripePricingError('Stripe unavailable'), True, False),
        ({'include_pricing': 'false'}, None, True, False),
    )
    @ddt.unpack
    def test_list_ssp_products_pricing_null_behaviors(
        self,
        query_params,
        stripe_side_effect,
        expect_price_null,
        expect_stripe_called,
    ):
        """Parametrized: pricing unavailable vs include_pricing=false behaviors."""
        academy_metadata = {
            'title': 'AI Academy',
            'long_name': 'AI Academy for Business',
            'description': 'Learn AI end-to-end',
            'marketing_url': 'https://example.com/ai',
            'thumbnail_url': 'https://cdn.example.com/ai.png',
        }

        with mock.patch(
            'enterprise_access.apps.customer_billing.models.get_cached_academy_data',
        ) as mock_get_cached_academy_data:
            with mock.patch(
                'enterprise_access.apps.api.v1.views.customer_billing.get_all_stripe_prices',
            ) as mock_get_all_stripe_prices:
                mock_get_cached_academy_data.return_value = academy_metadata
                if stripe_side_effect is not None:
                    mock_get_all_stripe_prices.side_effect = stripe_side_effect
                else:
                    mock_get_all_stripe_prices.return_value = {
                        'ai_academy_yearly_price': {'unit_amount_decimal': Decimal('149.00')}
                    }

            if query_params:
                response = self.client.get(self.list_url, query_params)
            else:
                response = self.client.get(self.list_url)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            payload = next(p for p in response.data if p['lookup_key'] ==
                           self.essentials_product.stripe_price_lookup_key)
            if expect_price_null:
                self.assertIsNone(payload['price'])
            else:
                self.assertIsNotNone(payload['price'])

            if expect_stripe_called:
                mock_get_all_stripe_prices.assert_called()
            else:
                mock_get_all_stripe_prices.assert_not_called()

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.get_all_stripe_prices')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_list_ssp_products_catalog_failure_returns_metadata_nulls(
        self,
        mock_get_cached_academy_data,
        mock_get_all_stripe_prices,
    ):
        """Catalog lookup failures should not raise 500 for the public endpoint."""
        mock_get_cached_academy_data.side_effect = Exception('catalog unavailable')

        response = self.client.get(f'{self.list_url}?include_pricing=false')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertIsNone(response.data[0]['name'])
        self.assertIsNone(response.data[0]['long_name'])
        self.assertIsNone(response.data[0]['description'])
        self.assertIsNone(response.data[0]['marketing_url'])
        self.assertIsNone(response.data[0]['thumbnail_url'])
        self.assertEqual(response.data[0]['lookup_key'], 'ai_academy_yearly_price')
        self.assertIsNone(response.data[0]['price'])
        mock_get_all_stripe_prices.assert_not_called()

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.get_all_stripe_prices')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_list_ssp_products_falls_back_to_stripe_product_fields(
        self,
        mock_get_cached_academy_data,
        mock_get_all_stripe_prices,
    ):
        """When academy metadata is unavailable, fallback fields come from Stripe Product."""
        mock_get_cached_academy_data.return_value = None
        mock_get_all_stripe_prices.return_value = {
            'ai_academy_yearly_price': {
                'unit_amount_decimal': Decimal('149.00'),
                'stripe_name': 'AI Essentials',
                'stripe_description': 'Learn core AI skills',
                'stripe_marketing_url': 'https://example.com/ai-essentials',
                'stripe_thumbnail_url': 'https://cdn.example.com/ai-essentials.png',
            }
        }

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['name'], 'AI Essentials')
        self.assertEqual(response.data[0]['long_name'], 'AI Essentials')
        self.assertEqual(response.data[0]['description'], 'Learn core AI skills')
        self.assertEqual(response.data[0]['marketing_url'], 'https://example.com/ai-essentials')
        self.assertEqual(response.data[0]['thumbnail_url'], 'https://cdn.example.com/ai-essentials.png')
        self.assertEqual(response.data[0]['price'], '149.00')

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.get_all_stripe_prices')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_list_ssp_products_batches_lookup_keys_for_pricing(
        self,
        mock_get_cached_academy_data,
        mock_get_all_stripe_prices,
    ):
        """Stripe lookups are chunked so all products receive pricing when keys exceed API filter limits."""
        mock_get_cached_academy_data.return_value = {
            'title': 'Academy',
            'long_name': 'Academy Long Name',
            'description': 'Description',
            'marketing_url': 'https://example.com/academy',
            'thumbnail_url': 'https://cdn.example.com/academy.png',
        }

        for index in range(2, 13):
            SspProduct.objects.create(
                slug=f'academy-{index}',
                stripe_price_lookup_key=f'lookup_{index}',
                academy_uuid=uuid.uuid4(),
                catalog_query_uuid=uuid.uuid4(),
                license_manager_product_id_trial=2,
                license_manager_product_id_paid=1,
                is_active=True,
            )

        mock_get_all_stripe_prices.return_value = {
            p.stripe_price_lookup_key: {'unit_amount_decimal': Decimal('100.00')}
            for p in SspProduct.objects.all()
        }

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 12)
        mock_get_all_stripe_prices.assert_called_once()
        for payload in response.data:
            self.assertEqual(payload['price'], '100.00')

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.get_all_stripe_prices')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_list_ssp_products_uses_full_scan_for_missing_lookup_key(
        self,
        mock_get_cached_academy_data,
        mock_get_all_stripe_prices,
    ):
        """If filtered lookup misses a key, full active-price scan should resolve it."""
        missing_lookup_key = 'essentials_tech_and_digital_transformation'
        self.essentials_product.stripe_price_lookup_key = missing_lookup_key
        self.essentials_product.save(update_fields=['stripe_price_lookup_key'])

        mock_get_cached_academy_data.return_value = {
            'title': 'Tech Academy',
            'long_name': 'Tech & Digital Transformation Academy',
            'description': 'Transform your organization',
            'marketing_url': 'https://example.com/tech',
            'thumbnail_url': 'https://cdn.example.com/tech.png',
        }

        mock_get_all_stripe_prices.return_value = {
            missing_lookup_key: {'unit_amount_decimal': Decimal('199.00')}
        }

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['lookup_key'], missing_lookup_key)
        self.assertEqual(response.data[0]['price'], '199.00')

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.get_all_stripe_prices')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_list_ssp_products_full_scan_stops_after_resolving_key_and_slug(
        self,
        mock_get_cached_academy_data,
        mock_get_all_stripe_prices,
    ):
        """Full active-price scan should stop once requested lookup key and slug are resolved."""
        missing_lookup_key = 'essentials_tech_and_digital_transformation'
        self.essentials_product.stripe_price_lookup_key = missing_lookup_key
        self.essentials_product.save(update_fields=['stripe_price_lookup_key'])

        mock_get_cached_academy_data.return_value = {
            'title': 'Tech Academy',
            'long_name': 'Tech & Digital Transformation Academy',
            'description': 'Transform your organization',
            'marketing_url': 'https://example.com/tech',
            'thumbnail_url': 'https://cdn.example.com/tech.png',
        }

        mock_get_all_stripe_prices.return_value = {
            missing_lookup_key: {'unit_amount_decimal': Decimal('199.00'), 'stripe_name': 'Resolved'}
        }

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['lookup_key'], missing_lookup_key)
        self.assertEqual(response.data[0]['price'], '199.00')

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.get_all_stripe_prices')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_list_ssp_products_uses_slug_metadata_when_lookup_key_drifted(
        self,
        mock_get_cached_academy_data,
        mock_get_all_stripe_prices,
    ):
        """If local lookup_key is stale, price can still resolve by Stripe metadata ssp_product_slug."""
        self.essentials_product.stripe_price_lookup_key = (
            'essentials_artificial_intelligence_subscription_license_yearly'
        )
        self.essentials_product.save(update_fields=['stripe_price_lookup_key'])

        mock_get_cached_academy_data.return_value = {
            'title': 'AI Academy',
            'long_name': 'AI Academy for Business',
            'description': 'Learn AI end-to-end',
            'marketing_url': 'https://example.com/ai',
            'thumbnail_url': 'https://cdn.example.com/ai.png',
        }

        # Structure the map using the active lookup key that the database record is expecting
        mock_get_all_stripe_prices.return_value = {
            'essentials_artificial_intelligence_subscription_license_yearly': {
                'unit_amount_decimal': Decimal('149.00'),
                'stripe_name': 'AI Academy',
            }
        }

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data[0]['lookup_key'],
            'essentials_artificial_intelligence_subscription_license_yearly',
        )
        self.assertEqual(response.data[0]['price'], '149.00')

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.get_all_stripe_prices')
    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.stripe.Price.list')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_retrieve_ssp_product_by_slug(
        self,
        mock_get_cached_academy_data,
        mock_price_list,
        mock_get_all_stripe_prices,
    ):
        """Retrieve resolves an academy product by slug and returns serialized payload."""
        detail_url = reverse('api:v1:ssp-products-detail', kwargs={'slug': 'ai-academy-yearly'})
        mock_get_cached_academy_data.return_value = {
            'title': 'AI Academy',
            'long_name': 'AI Academy for Business',
            'description': 'Learn AI end-to-end',
            'marketing_url': 'https://example.com/ai',
            'thumbnail_url': 'https://cdn.example.com/ai.png',
        }
        mock_price_list.return_value = mock.Mock(
            data=[self._mock_price('ai_academy_yearly_price', 14900)],
        )
        # Supply mock data for master dictionary
        mock_get_all_stripe_prices.return_value = {
            'ai_academy_yearly_price': {
                'unit_amount_decimal': Decimal('149.00'),
                'stripe_name': 'AI Essentials',
            }
        }

        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['lookup_key'], 'ai_academy_yearly_price')
        # Pricing is not populated on retrieve in current view implementation
        self.assertIsNone(response.data['price'])

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.get_all_stripe_prices')
    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.stripe.Price.list')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_retrieve_ssp_product_by_lookup_key_db_match(
        self,
        mock_get_cached_academy_data,
        mock_price_list,
        mock_get_all_stripe_prices,
    ):
        """Retrieve resolves when the path slug equals the Stripe lookup_key stored on the DB product."""
        detail_url = reverse(
            'api:v1:ssp-products-detail',
            kwargs={
                'slug': self.essentials_product.stripe_price_lookup_key,
            },
        )
        mock_get_cached_academy_data.return_value = {
            'title': 'AI Academy',
            'long_name': 'AI Academy for Business',
            'description': 'Learn AI end-to-end',
            'marketing_url': 'https://example.com/ai',
            'thumbnail_url': 'https://cdn.example.com/ai.png',
        }
        mock_price_list.return_value = mock.Mock(
            data=[self._mock_price(self.essentials_product.stripe_price_lookup_key, 14900)],
        )
        mock_get_all_stripe_prices.return_value = {
            self.essentials_product.stripe_price_lookup_key: {
                'unit_amount_decimal': Decimal('149.00'),
                'stripe_name': 'AI Essentials',
            }
        }

        # Sanity-check DB state before exercising the view
        self.assertEqual(
            SspProduct.objects.filter(
                stripe_price_lookup_key=self.essentials_product.stripe_price_lookup_key,
                is_active=True,
            ).count(),
            1,
        )

        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['lookup_key'], self.essentials_product.stripe_price_lookup_key)
        # Pricing is not populated on retrieve in current view implementation
        self.assertIsNone(response.data['price'])

    @override_settings(SSP_ESSENTIALS_THUMBNAIL_S3_BASE_URL=None)
    def test_build_public_thumbnail_url_no_base_setting(self):
        """Test relative thumbnail path returns as-is when base URL setting is missing."""

        serializer = SspEssentialsProductResponseSerializer()
        relative_path = "academies/ai/thumbnail.png"
        result = serializer._build_public_thumbnail_url(relative_path)
        self.assertEqual(result, relative_path)

    def test_get_price_handles_malformed_decimal_exception(self):
        """Test get_price handles formatting exceptions gracefully by returning None."""

        class FakeProduct:
            """Fake product wrapper for pricing structure validation."""

        product = FakeProduct()
        serializer = SspEssentialsProductResponseSerializer()

        with mock.patch.object(serializer, '_price_data', return_value={'unit_amount_decimal': 'garbage_string'}):
            self.assertIsNone(serializer.get_price(product))

        with mock.patch.object(serializer, '_price_data', return_value={'unit_amount_decimal': {'invalid': 'type'}}):
            self.assertIsNone(serializer.get_price(product))

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.stripe.Price.list')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_retrieve_resolves_via_direct_stripe_lookup(
        self,
        mock_get_cached_academy_data,
        mock_price_list,
    ):
        """Unknown slug can resolve through direct Stripe lookup_key matching."""
        mapped_product = SspProduct.objects.create(
            slug='direct-stripe-slug',
            stripe_price_lookup_key='direct_stripe_lookup_key',
            academy_uuid=uuid.uuid4(),
            catalog_query_uuid=uuid.uuid4(),
            license_manager_product_id_trial=2,
            license_manager_product_id_paid=1,
            is_active=True,
        )
        requested_slug = 'unknown_lookup_alias'
        detail_url = reverse('api:v1:ssp-products-detail', kwargs={'slug': requested_slug})

        mock_get_cached_academy_data.return_value = {
            'title': 'Direct Academy',
            'long_name': 'Direct Academy Long',
            'description': 'Direct Academy description',
            'marketing_url': 'https://example.com/direct',
            'thumbnail_url': 'https://cdn.example.com/direct.png',
        }
        mock_price_list.return_value = mock.Mock(
            data=[self._mock_price(mapped_product.stripe_price_lookup_key, 2500)]
        )

        # The retrieve endpoint no longer performs dynamic Stripe resolution; expect 404
        response = self.client.get(detail_url, {'include_pricing': 'false'})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        mock_price_list.assert_not_called()

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.stripe.Price.list')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_retrieve_resolves_via_non_active_stripe_lookup_fallback(
        self,
        mock_get_cached_academy_data,
        mock_price_list,
    ):
        """When active Stripe lookup misses, retrieve falls back to non-active lookup."""
        mapped_product = SspProduct.objects.create(
            slug='fallback-stripe-slug',
            stripe_price_lookup_key='fallback_stripe_lookup_key',
            academy_uuid=uuid.uuid4(),
            catalog_query_uuid=uuid.uuid4(),
            license_manager_product_id_trial=2,
            license_manager_product_id_paid=1,
            is_active=True,
        )
        requested_slug = 'fallback_requested_lookup'
        detail_url = reverse('api:v1:ssp-products-detail', kwargs={'slug': requested_slug})

        mock_get_cached_academy_data.return_value = {
            'title': 'Fallback Academy',
            'long_name': 'Fallback Academy Long',
            'description': 'Fallback Academy description',
            'marketing_url': 'https://example.com/fallback',
            'thumbnail_url': 'https://cdn.example.com/fallback.png',
        }
        mock_price_list.side_effect = [
            mock.Mock(data=[]),
            mock.Mock(data=[self._mock_price(mapped_product.stripe_price_lookup_key, 2500)]),
        ]

        # The retrieve endpoint no longer performs dynamic Stripe resolution; expect 404
        response = self.client.get(detail_url, {'include_pricing': 'false'})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(mock_price_list.call_count, 0)

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.stripe.Price.list')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_retrieve_resolves_via_full_scan_metadata_match(
        self,
        mock_get_cached_academy_data,
        mock_price_list,
    ):
        """When direct Stripe lookup misses, retrieve can resolve by metadata.ssp_product_slug."""
        mapped_product = SspProduct.objects.create(
            slug='scan-metadata-slug',
            stripe_price_lookup_key='scan_metadata_lookup_key',
            academy_uuid=uuid.uuid4(),
            catalog_query_uuid=uuid.uuid4(),
            license_manager_product_id_trial=2,
            license_manager_product_id_paid=1,
            is_active=True,
        )
        requested_slug = 'requested_slug_for_metadata_match'
        detail_url = reverse('api:v1:ssp-products-detail', kwargs={'slug': requested_slug})

        mock_get_cached_academy_data.return_value = {
            'title': 'Scan Academy',
            'long_name': 'Scan Academy Long',
            'description': 'Scan Academy description',
            'marketing_url': 'https://example.com/scan',
            'thumbnail_url': 'https://cdn.example.com/scan.png',
        }
        full_scan_response = mock.Mock(
            auto_paging_iter=mock.Mock(
                return_value=iter([
                    mock.Mock(
                        lookup_key=mapped_product.stripe_price_lookup_key,
                        metadata={'ssp_product_slug': requested_slug},
                    ),
                ])
            )
        )
        mock_price_list.side_effect = [
            mock.Mock(data=[]),
            mock.Mock(data=[]),
            full_scan_response,
        ]

        # The retrieve endpoint no longer performs a full Stripe scan; expect 404
        response = self.client.get(detail_url, {'include_pricing': 'false'})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.stripe.Price.list')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_retrieve_resolves_via_full_scan_fuzzy_lookup_match(
        self,
        mock_get_cached_academy_data,
        mock_price_list,
    ):
        """Full scan fallback also resolves when requested slug is contained in Stripe lookup_key."""
        requested_slug = 'essentials_artificial_intelligence_subscription_license_yearly'
        mapped_lookup_key = f'{requested_slug}_v2'
        _ = SspProduct.objects.create(
            slug='scan-fuzzy-slug',
            stripe_price_lookup_key=mapped_lookup_key,
            academy_uuid=uuid.uuid4(),
            catalog_query_uuid=uuid.uuid4(),
            license_manager_product_id_trial=2,
            license_manager_product_id_paid=1,
            is_active=True,
        )
        detail_url = reverse('api:v1:ssp-products-detail', kwargs={'slug': requested_slug})

        mock_get_cached_academy_data.return_value = {
            'title': 'Fuzzy Academy',
            'long_name': 'Fuzzy Academy Long',
            'description': 'Fuzzy Academy description',
            'marketing_url': 'https://example.com/fuzzy',
            'thumbnail_url': 'https://cdn.example.com/fuzzy.png',
        }
        full_scan_response = mock.Mock(
            auto_paging_iter=mock.Mock(
                return_value=iter([
                    mock.Mock(
                        lookup_key=mapped_lookup_key,
                        metadata={},
                    ),
                ])
            )
        )
        mock_price_list.side_effect = [
            mock.Mock(data=[]),
            mock.Mock(data=[]),
            full_scan_response,
        ]

        # The retrieve endpoint no longer performs a full Stripe scan; expect 404
        response = self.client.get(detail_url, {'include_pricing': 'false'})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.stripe.Price.list')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_retrieve_returns_404_when_stripe_resolution_fails(
        self,
        mock_get_cached_academy_data,
        mock_price_list,
    ):
        """Unknown lookup slug returns 404 when Stripe lookup and full scan cannot resolve."""
        requested_slug = 'requested_slug_unresolvable'
        detail_url = reverse('api:v1:ssp-products-detail', kwargs={'slug': requested_slug})
        mock_get_cached_academy_data.return_value = {
            'title': 'Unused Academy',
            'long_name': 'Unused Academy Long',
            'description': 'Unused Academy description',
            'marketing_url': 'https://example.com/unused',
            'thumbnail_url': 'https://cdn.example.com/unused.png',
        }
        mock_price_list.side_effect = stripe.error.APIConnectionError('boom')

        response = self.client.get(detail_url, {'include_pricing': 'false'})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.get_all_stripe_prices')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_retrieve_teams_slug_returns_404(
        self,
        mock_get_cached_academy_data,
        mock_get_all_stripe_prices,
    ):
        """Teams products are excluded from this endpoint and should 404 by slug."""
        detail_url = reverse('api:v1:ssp-products-detail', kwargs={'slug': 'teams-yearly'})

        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        mock_get_cached_academy_data.assert_not_called()
        mock_get_all_stripe_prices.assert_not_called()

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.stripe.Price.list')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_retrieve_ssp_product_include_pricing_false(
        self,
        mock_get_cached_academy_data,
        mock_price_list,
    ):
        """Retrieve honors include_pricing=false and skips Stripe call."""
        detail_url = reverse('api:v1:ssp-products-detail', kwargs={'slug': 'ai-academy-yearly'})
        mock_get_cached_academy_data.return_value = {
            'title': 'AI Academy',
            'long_name': 'AI Academy for Business',
            'description': 'Learn AI end-to-end',
            'marketing_url': 'https://example.com/ai',
            'thumbnail_url': 'https://cdn.example.com/ai.png',
        }

        response = self.client.get(detail_url, {'include_pricing': 'false'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['lookup_key'], 'ai_academy_yearly_price')
        self.assertIsNone(response.data['price'])
        mock_price_list.assert_not_called()

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.stripe.Price.list')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_retrieve_ssp_product_by_slug_when_slug_matches_db(self, mock_get_cached_academy_data, mock_price_list):
        """Retrieve resolves when the path slug matches the DB `slug`."""
        # create a product with a known lookup_key
        SspProduct.objects.create(
            slug='public-mapped-slug',
            stripe_price_lookup_key='mapped_lookup_key',
            academy_uuid=uuid.uuid4(),
            catalog_query_uuid=uuid.uuid4(),
            license_manager_product_id_trial=2,
            license_manager_product_id_paid=1,
            is_active=True,
        )

        mock_get_cached_academy_data.return_value = {
            'title': 'Mapped Academy',
            'long_name': 'Mapped Academy Long',
            'description': 'Mapped desc',
            'marketing_url': 'https://example.com/mapped',
            'thumbnail_url': 'https://cdn.example.com/mapped.png',
        }
        mock_price_list.return_value = mock.Mock(data=[self._mock_price('mapped_lookup_key', 2500)])

        detail_url = reverse('api:v1:ssp-products-detail', kwargs={'slug': 'public-mapped-slug'})
        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['lookup_key'], 'mapped_lookup_key')

    # Removed test_retrieve_falls_back_to_settings_lookup_when_slug_missing: settings-based fallback no longer used

    # Internal helper-based tests removed: helpers were refactored out of the view.

    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.get_all_stripe_prices')
    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.TieredCache.get_cached_response')
    @mock.patch('enterprise_access.apps.api.v1.views.customer_billing.stripe.Price.list')
    @mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
    def test_retrieve_uses_cached_all_prices_to_resolve(
        self,
        mock_get_cached_academy_data,
        mock_price_list,
        mock_get_cached_response,
        mock_get_all_stripe_prices,
    ):
        """When cache is populated, view should consult cached mapping to resolve slug."""
        requested_slug = 'cached_lookup_slug'
        mapped_lookup_key = 'cached_mapped_lookup'
        SspProduct.objects.create(
            slug='cached-product',
            stripe_price_lookup_key=mapped_lookup_key,
            academy_uuid=uuid.uuid4(),
            catalog_query_uuid=uuid.uuid4(),
            license_manager_product_id_trial=2,
            license_manager_product_id_paid=1,
            is_active=True,
        )
        detail_url = reverse('api:v1:ssp-products-detail', kwargs={'slug': requested_slug})

        mock_get_cached_academy_data.return_value = {
            'title': 'Cached Academy',
            'long_name': 'Cached Academy Long',
            'description': 'Cached Academy description',
            'marketing_url': 'https://example.com/cached',
            'thumbnail_url': 'https://cdn.example.com/cached.png',
        }

        # Simulate initial Stripe lookups miss (active then non-active)
        mock_price_list.side_effect = [mock.Mock(data=[]), mock.Mock(data=[])]

        # Simulate TieredCache indicating a cached value exists and return mapping
        mock_get_cached_response.return_value = mock.Mock(is_found=True)
        mock_get_all_stripe_prices.return_value = {
            mapped_lookup_key: {'product': {'metadata': {'ssp_product_slug': requested_slug}}}
        }

        # The retrieve endpoint no longer consults cached mapping for resolution; expect 404
        response = self.client.get(detail_url, {'include_pricing': 'false'})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_metadata_value_and_payment_helpers(self):
        """Test metadata_value extraction and billing management helpers."""
        # metadata_value: non-dict object that raises KeyError/TypeError
        class BadMeta:
            """A metadata-like object that raises on lookup to exercise error handling."""
            def __getitem__(self, key):
                raise KeyError()

        assert SspProductViewSet._metadata_value({'a': 1}, 'a') == 1
        assert SspProductViewSet._metadata_value(BadMeta(), 'a') is None

        # normalize invoice status
        assert BillingManagementViewSet._normalize_invoice_status('paid') == 'paid'
        assert BillingManagementViewSet._normalize_invoice_status('open') == 'open'
        assert BillingManagementViewSet._normalize_invoice_status('void') == 'void'
        assert BillingManagementViewSet._normalize_invoice_status('uncollectible') == 'uncollectible'
        assert BillingManagementViewSet._normalize_invoice_status('weird') == 'open'

        # yearly amount calculation with explicit unit_amount and recurring interval
        class FakeSubscription:
            """Minimal fake subscription used for yearly amount and license count tests."""
            def __init__(self, items):
                self._items = items

            def to_dict(self):
                return {'items': {'data': self._items}}

        # yearly
        sub = FakeSubscription([{'price': {'unit_amount': 100, 'recurring': {'interval': 'year'}}, 'quantity': 2}])
        assert BillingManagementViewSet._get_yearly_amount(sub) == 100 * 2

        # monthly -> multiply by 12
        sub = FakeSubscription([{'price': {'unit_amount': 10, 'recurring': {'interval': 'month'}}, 'quantity': 3}])
        assert BillingManagementViewSet._get_yearly_amount(sub) == (10 * 12) * 3

        # missing unit_amount triggers stripe.Price.retrieve
        class P:
            """Simple price-like object returned by fake retrieve."""
            def to_dict(self):
                return {'unit_amount': 200, 'recurring': {'interval': 'year'}}

        # Use mock.patch.object context manager instead of the pytest monkeypatch argument
        with mock.patch.object(stripe.Price, 'retrieve', staticmethod(lambda _id: P())):
            sub = FakeSubscription([{'price': {'id': 'price_1'}, 'quantity': 1}])
            assert BillingManagementViewSet._get_yearly_amount(sub) == 200

        # license count
        sub = FakeSubscription([{'quantity': 1}, {'quantity': 4}])
        assert BillingManagementViewSet._get_license_count(sub) == 5

# Internal helper-based tests removed: helpers were refactored out of the view.


@mock.patch('enterprise_access.apps.api.v1.views.customer_billing.stripe.Price.list')
@mock.patch('enterprise_access.apps.customer_billing.models.get_cached_academy_data')
@pytest.mark.django_db
def test_retrieve_resolves_when_stripe_returns_metadata_product(
    mock_get_cached_academy_data,
    mock_price_list,
):
    """When Stripe single-lookup returns a price with
    metadata.ssp_product_slug, retrieve should resolve the DB product."""
    mapped_product = SspProduct.objects.create(
        slug='meta-mapped-slug',
        stripe_price_lookup_key='meta_mapped_lookup_key',
        academy_uuid=uuid.uuid4(),
        catalog_query_uuid=uuid.uuid4(),
        license_manager_product_id_trial=2,
        license_manager_product_id_paid=1,
        is_active=True,
    )

    requested_slug = 'alias_for_meta'
    detail_url = reverse('api:v1:ssp-products-detail', kwargs={'slug': requested_slug})

    mock_get_cached_academy_data.return_value = {
        'title': 'Meta Academy',
        'long_name': 'Meta Academy Long',
        'description': 'Meta Academy description',
        'marketing_url': 'https://example.com/meta',
        'thumbnail_url': 'https://cdn.example.com/meta.png',
    }

    # Stripe returns a price whose lookup_key matches our DB product
    mock_price_list.return_value = mock.Mock(
        data=[
            mock.Mock(
                product={},
                metadata={},
                lookup_key=mapped_product.stripe_price_lookup_key,
                unit_amount=1000,
            ),
        ],
    )

    factory = APIRequestFactory()
    request = Request(factory.get(detail_url))
    vs = SspProductViewSet()
    vs.kwargs = {'slug': requested_slug}
    vs.request = request
    vs.format_kwarg = None
    # The view no longer attempts Stripe lookups on retrieve; expect Http404
    with pytest.raises(Http404):
        vs.retrieve(request, slug=requested_slug)
