"""
Tests for BFF serializers.
"""
import ddt

from enterprise_access.apps.bffs.serializers import EnterpriseCustomerSerializer
from enterprise_access.apps.bffs.tests.utils import TestHandlerContextMixin


@ddt.ddt
class TestEnterpriseCustomerSerializer(TestHandlerContextMixin):
    """
    Tests for EnterpriseCustomerSerializer.
    """

    @ddt.data(True, False)
    def test_show_non_production_banner_passed_through(self, show_non_production_banner):
        """
        The `show_non_production_banner` flag sourced from the LMS is included in the serialized output.

        Without it, the learner portal cannot render its non-production banner on BFF-backed routes
        (e.g. the dashboard), even though the flag is present on non-BFF routes that read from the LMS
        `enterprise-learner` endpoint directly.
        """
        enterprise_customer = {
            **self.mock_enterprise_customer,
            'show_non_production_banner': show_non_production_banner,
        }

        serialized_enterprise_customer = EnterpriseCustomerSerializer(enterprise_customer).data

        self.assertEqual(
            serialized_enterprise_customer['show_non_production_banner'],
            show_non_production_banner,
        )

    def test_show_non_production_banner_defaults_to_false_when_missing(self):
        """
        LMS payloads that predate the field (e.g. a response cached from before the edx-enterprise
        release that added it) serialize to False rather than raising.
        """
        enterprise_customer = {
            key: value
            for key, value in self.mock_enterprise_customer.items()
            if key != 'show_non_production_banner'
        }

        serialized_enterprise_customer = EnterpriseCustomerSerializer(enterprise_customer).data

        self.assertFalse(serialized_enterprise_customer['show_non_production_banner'])

    def test_show_non_production_banner_defaults_to_false_on_validation_path(self):
        """
        The default also applies on the `data=` path, which is the one production hits via
        `BaseResponseBuilder.serialize`.

        This matters because `serialize` does not re-raise on validation failure: it degrades the
        whole response to an unvalidated echo plus a warning. A missing key must therefore validate
        cleanly rather than take every other field down with it.
        """
        enterprise_customer = {
            key: value
            for key, value in self.mock_enterprise_customer.items()
            if key != 'show_non_production_banner'
        }

        serializer = EnterpriseCustomerSerializer(data=enterprise_customer)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertFalse(serializer.validated_data['show_non_production_banner'])
        self.assertFalse(serializer.data['show_non_production_banner'])
