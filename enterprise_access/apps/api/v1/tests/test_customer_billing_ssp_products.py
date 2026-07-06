"""Tests for SSP Essentials products API endpoint."""
# pylint: disable=protected-access
import uuid
from decimal import Decimal
from unittest import mock

import ddt
import stripe
from django.test import override_settings
from django.urls import reverse
from rest_framework import status

from enterprise_access.apps.api.serializers.customer_billing import SspEssentialsProductResponseSerializer
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
            'tags': ['ai', 'leadership'],
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
        self.assertEqual(essentials_payload['tags'], ['ai', 'leadership'])
        self.assertEqual(essentials_payload['price'], '149.00')

        mock_get_all_stripe_prices.assert_called_once()

        detail_url = reverse('api:v1:ssp-products-detail', kwargs={'slug': 'ai-academy-yearly'})
        detail_response = self.client.get(detail_url)
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_response.data['tags'], ['ai', 'leadership'])

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
        ({}, StripePricingError('Stripe unavailable'), True, True),
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
                payload = next(
                    p for p in response.data
                    if p['lookup_key'] == self.essentials_product.stripe_price_lookup_key)
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
        self.assertEqual(response.data[0]['tags'], [])
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
        self.assertEqual(response.data[0]['tags'], [])
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
