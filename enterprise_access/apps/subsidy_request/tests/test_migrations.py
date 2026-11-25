"""
Tests for subsidy_request schema migrations, using django_test_migrations.
"""
from uuid import uuid4

from django.db import connections
from django.test import TransactionTestCase
from django_test_migrations.migrator import Migrator

BEFORE_MULTI_PLAN = ('subsidy_request', '0022_mariadb_uuid_conversion')
ADD_MULTI_PLAN = ('subsidy_request', '0023_subsidyrequestcustomerconfiguration_enable_multi_plan_funding')


def _field_names(state, model_name):
    model = state.apps.get_model('subsidy_request', model_name)
    return {field.name for field in model._meta.get_fields()}


class TestAddEnableMultiPlanFunding(TransactionTestCase):
    """
    Tests for migration 0023, which adds ``enable_multi_plan_funding`` to SubsidyRequestCustomerConfiguration and
    its history table.
    """

    def setUp(self):
        super().setUp()
        self.migrator = Migrator(database='default')

    def tearDown(self):
        connections['default'].close()
        self.migrator.reset()
        super().tearDown()

    def test_applies_with_existing_rows_off_and_reverses_cleanly(self):
        old_state = self.migrator.apply_initial_migration(BEFORE_MULTI_PLAN)
        customer_uuid = uuid4()
        old_state.apps.get_model('subsidy_request', 'SubsidyRequestCustomerConfiguration').objects.create(
            enterprise_customer_uuid=customer_uuid,
        )

        new_state = self.migrator.apply_tested_migration(ADD_MULTI_PLAN)
        config_model = new_state.apps.get_model('subsidy_request', 'SubsidyRequestCustomerConfiguration')
        assert config_model.objects.get(enterprise_customer_uuid=customer_uuid).enable_multi_plan_funding is False
        assert 'enable_multi_plan_funding' in _field_names(new_state, 'HistoricalSubsidyRequestCustomerConfiguration')

        reverted_state = self.migrator.apply_tested_migration(BEFORE_MULTI_PLAN)
        for model_name in ('SubsidyRequestCustomerConfiguration', 'HistoricalSubsidyRequestCustomerConfiguration'):
            assert 'enable_multi_plan_funding' not in _field_names(reverted_state, model_name)
        reverted_model = reverted_state.apps.get_model('subsidy_request', 'SubsidyRequestCustomerConfiguration')
        assert reverted_model.objects.filter(enterprise_customer_uuid=customer_uuid).exists()
