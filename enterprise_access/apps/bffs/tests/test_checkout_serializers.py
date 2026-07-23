from django.test import TestCase

from enterprise_access.apps.bffs.checkout.serializers import CheckoutIntentMinimalResponseSerializer


class TestCheckoutIntentMinimalResponseSerializer(TestCase):
    """
    Unit tests for CheckoutIntentMinimalResponseSerializer.
    Tests the serializer contract directly, independent of the builder,
    so contract regressions are caught at the source.
    """

    def _valid_payload(self, **overrides):
        base = {
            'id': 1,
            'uuid': 'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
            'state': 'created',
            'expires_at': '2026-12-31T00:00:00Z',
            'enterprise_slug': 'test-enterprise',
            'quantity': 5,
            'stripe_checkout_session_id': 'cs_test_123abc',
            'last_checkout_error': '',
            'last_provisioning_error': '',
            'admin_portal_url': 'https://portal.edx.org/test-enterprise',
            'ssp_product': 'data-academy-yearly',
        }
        base.update(overrides)
        return base

    def test_ssp_product_present_and_serialized(self):
        """ssp_product must appear in serialized output when provided."""
        serializer = CheckoutIntentMinimalResponseSerializer(
            data=self._valid_payload()
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIn('ssp_product', serializer.validated_data)
        self.assertEqual(serializer.validated_data['ssp_product'], 'data-academy-yearly')

    def test_ssp_product_null_is_invalid(self):
        """ssp_product=None must be rejected (ssp_product is non-nullable on CheckoutIntent)."""
        serializer = CheckoutIntentMinimalResponseSerializer(
            data=self._valid_payload(ssp_product=None)
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('ssp_product', serializer.errors)

    def test_ssp_product_absent_is_invalid(self):
        """ssp_product omitted entirely must produce a validation error."""
        payload = self._valid_payload()
        payload.pop('ssp_product')
        serializer = CheckoutIntentMinimalResponseSerializer(data=payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn('ssp_product', serializer.errors)

    def test_uuid_required(self):
        """
        uuid is a required field. Omitting it must produce a validation
        error — guards against silent-degradation where the builder returns
        a partially-invalid payload without raising.
        """
        payload = self._valid_payload()
        payload.pop('uuid')
        serializer = CheckoutIntentMinimalResponseSerializer(data=payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn('uuid', serializer.errors)   # error must be on uuid, not id
