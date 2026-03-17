"""
Test scenarios for billing management API.

Each test scenario is a callable that tests a specific API functionality.
"""
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .client import BillingManagementClient
from .config import Config


logger = logging.getLogger(__name__)


@dataclass
class TestScenario:
    """
    Definition of a test scenario.

    Attributes:
        name: Unique identifier for the test
        description: Human-readable description of what the test does
        run: Callable that executes the test
        depends_on: Optional list of test names that must pass first
        skip_on_error: If True, continue with other tests even if this fails
    """
    name: str
    description: str
    run: Callable[[BillingManagementClient, Config], Dict[str, Any]]
    depends_on: Optional[List[str]] = None
    skip_on_error: bool = False


# Test implementations

def test_health_check(client: BillingManagementClient, config: Config) -> Dict[str, Any]:
    """Test the health check endpoint."""
    logger.info("Testing health check endpoint")
    result = client.health_check()

    assert 'status' in result, "Health check response missing 'status' field"
    assert result['status'] == 'healthy', f"Unexpected health status: {result['status']}"

    return {'status': 'PASS', 'data': result}


def test_get_address(client: BillingManagementClient, config: Config) -> Dict[str, Any]:
    """Test getting billing address."""
    logger.info(f"Getting address for enterprise {config.enterprise_customer_uuid}")
    result = client.get_address(config.enterprise_customer_uuid)

    # Address may be partially filled or empty, just verify structure
    expected_fields = ['name', 'email', 'phone', 'country', 'address_line_1',
                       'address_line_2', 'city', 'state', 'postal_code']
    for field in expected_fields:
        assert field in result, f"Address response missing field: {field}"

    logger.info(f"Retrieved address for {result.get('name', 'N/A')}")
    return {'status': 'PASS', 'data': result}


def test_update_address(client: BillingManagementClient, config: Config) -> Dict[str, Any]:
    """Test updating billing address and verify cache invalidation."""
    logger.info("Updating billing address and verifying cache invalidation")

    # Step 1: Get current address (this will cache it)
    logger.info("Step 1: Getting current address (will be cached)")
    original = client.get_address(config.enterprise_customer_uuid)
    logger.info(f"Current address name: {original.get('name')}")

    # Step 2: Update address with new data
    logger.info("Step 2: Updating address with new data")
    new_address = {
        'name': 'Test Company Integration Tests',
        'email': 'billing-integration@test.example.com',
        'phone': '+1-555-0123',
        'country': 'US',
        'address_line_1': '123 Integration Test Street',
        'address_line_2': 'Suite 456',
        'city': 'Test City',
        'state': 'CA',
        'postal_code': '94105',
    }

    update_result = client.update_address(config.enterprise_customer_uuid, new_address)

    # Verify update response has correct data
    assert update_result['name'] == new_address['name'], "Name not updated correctly in response"
    assert update_result['email'] == new_address['email'], "Email not updated correctly in response"
    assert update_result['address_line_1'] == new_address['address_line_1'], "Address not updated in response"
    logger.info(f"Updated address name: {update_result.get('name')}")

    # Step 3: Get address again to verify cache was invalidated
    logger.info("Step 3: Getting address again (should NOT return cached data)")
    refreshed = client.get_address(config.enterprise_customer_uuid)

    # Step 4: Verify refreshed data matches updated data (proves cache invalidation worked)
    assert refreshed['name'] == new_address['name'], \
        f"Cache invalidation failed! Expected name '{new_address['name']}', got '{refreshed['name']}'"
    assert refreshed['email'] == new_address['email'], \
        f"Cache invalidation failed! Expected email '{new_address['email']}', got '{refreshed['email']}'"
    assert refreshed['address_line_1'] == new_address['address_line_1'], \
        f"Cache invalidation failed! Expected address '{new_address['address_line_1']}', got '{refreshed['address_line_1']}'"

    logger.info("✓ Cache invalidation verified - GET after UPDATE returned fresh data")
    logger.info(f"Refreshed address name: {refreshed.get('name')}")

    return {
        'status': 'PASS',
        'data': {
            'original': original,
            'updated': update_result,
            'refreshed': refreshed,
            'cache_invalidation_verified': True
        }
    }


def test_list_payment_methods(client: BillingManagementClient, config: Config) -> Dict[str, Any]:
    """Test listing payment methods and verify status field (GAP-002)."""
    logger.info("Listing payment methods and verifying status field")
    result = client.list_payment_methods(config.enterprise_customer_uuid)

    assert 'payment_methods' in result, "Response missing 'payment_methods' field"
    payment_methods = result['payment_methods']
    assert isinstance(payment_methods, list), "payment_methods should be a list"

    logger.info(f"Found {len(payment_methods)} payment method(s)")

    # GAP-002: Verify status field exists and has valid values
    valid_statuses = {'verified', 'pending', 'failed'}
    for pm in payment_methods:
        assert 'status' in pm, f"Payment method {pm.get('id')} missing 'status' field (GAP-002)"
        status = pm.get('status')
        assert status in valid_statuses, \
            f"Payment method {pm.get('id')} has invalid status '{status}'. " \
            f"Expected one of {valid_statuses} (GAP-002)"

        logger.debug(
            f"  - {pm.get('id')}: {pm.get('type', 'N/A')} "
            f"****{pm.get('last4', 'N/A')} "
            f"status={status} "
            f"({'default' if pm.get('is_default') else 'not default'})"
        )

    logger.info("✓ GAP-002: All payment methods have valid status field")

    return {
        'status': 'PASS',
        'data': result,
        'payment_method_count': len(payment_methods),
        'gap_002_verified': True
    }


def test_attach_payment_method_endpoint(
    client: BillingManagementClient,
    config: Config
) -> Dict[str, Any]:
    """Test attach payment method endpoint exists and handles errors properly (GAP-001)."""
    logger.info("Testing attach payment method endpoint (GAP-001)")

    # Test 1: Verify endpoint rejects missing payment_method_id
    logger.info("Test 1: Verify endpoint requires payment_method_id")
    response = client._request(
        'POST',
        'billing-management/payment-methods/',
        params={'enterprise_customer_uuid': config.enterprise_customer_uuid},
        json={},  # Missing payment_method_id
        log_response=False
    )
    # Should get 400 for missing payment_method_id
    assert response.status_code == 400, \
        f"Expected 400 for missing payment_method_id, got {response.status_code}"
    logger.info("✓ Endpoint correctly rejects missing payment_method_id with 400")

    # Test 2: Verify endpoint returns 404 for invalid payment method
    logger.info("Test 2: Verify endpoint returns 404 for non-existent payment method")
    response = client._request(
        'POST',
        'billing-management/payment-methods/',
        params={'enterprise_customer_uuid': config.enterprise_customer_uuid},
        json={'payment_method_id': 'pm_invalid_does_not_exist_12345'},
        log_response=False
    )
    # Should get 404 for invalid payment method
    assert response.status_code == 404, \
        f"Expected 404 for invalid payment method, got {response.status_code}"
    logger.info("✓ Endpoint correctly returns 404 for invalid payment method")

    logger.info("✓ GAP-001: Attach payment method endpoint exists and handles errors correctly")

    return {
        'status': 'PASS',
        'data': {
            'endpoint_exists': True,
            'validates_input': True,
            'handles_invalid_pm': True,
        },
        'gap_001_verified': True
    }


def test_list_transactions(client: BillingManagementClient, config: Config) -> Dict[str, Any]:
    """Test listing transactions."""
    logger.info("Listing transactions")
    result = client.list_transactions(config.enterprise_customer_uuid, limit=10)

    assert 'transactions' in result, "Response missing 'transactions' field"
    transactions = result['transactions']
    assert isinstance(transactions, list), "transactions should be a list"

    logger.info(f"Found {len(transactions)} transaction(s)")

    # Log transaction details
    for txn in transactions:
        logger.debug(
            f"  - {txn.get('id')}: {txn.get('status')} "
            f"${txn.get('amount_paid', 0)/100:.2f} on {txn.get('created')}"
        )

    return {
        'status': 'PASS',
        'data': result,
        'transaction_count': len(transactions),
        'has_more': 'next_page_token' in result
    }


def test_list_transactions_pagination(
    client: BillingManagementClient,
    config: Config
) -> Dict[str, Any]:
    """Test transaction pagination."""
    logger.info("Testing transaction pagination")

    # Get first page
    page1 = client.list_transactions(config.enterprise_customer_uuid, limit=1)
    page1_transactions = page1.get('transactions', [])

    result_data = {
        'page1_count': len(page1_transactions),
        'has_next_page': 'next_page_token' in page1
    }

    # If there's a next page, fetch it
    if 'next_page_token' in page1:
        logger.info("Fetching second page of transactions")
        page2 = client.list_transactions(
            config.enterprise_customer_uuid,
            limit=1,
            page_token=page1['next_page_token']
        )
        page2_transactions = page2.get('transactions', [])
        result_data['page2_count'] = len(page2_transactions)

        # Verify pages don't overlap
        page1_ids = {t['id'] for t in page1_transactions}
        page2_ids = {t['id'] for t in page2_transactions}
        overlap = page1_ids & page2_ids
        assert not overlap, f"Pages should not overlap, but found: {overlap}"

        logger.info(
            f"Pagination working: page1={len(page1_transactions)}, "
            f"page2={len(page2_transactions)}"
        )
    else:
        logger.info("No additional pages available")

    return {'status': 'PASS', 'data': result_data}


def test_get_subscription(client: BillingManagementClient, config: Config) -> Dict[str, Any]:
    """Test getting subscription details."""
    logger.info("Getting subscription details")
    result = client.get_subscription(config.enterprise_customer_uuid)

    if result is None:
        logger.info("No active subscription found")
        return {
            'status': 'PASS',
            'data': {'subscription': None},
            'has_subscription': False
        }

    # Verify subscription structure
    expected_fields = ['id', 'status', 'plan_type', 'cancel_at_period_end', 'current_period_end']
    for field in expected_fields:
        assert field in result, f"Subscription missing field: {field}"

    logger.info(
        f"Subscription: {result.get('plan_type')} - {result.get('status')} "
        f"(cancel_at_period_end={result.get('cancel_at_period_end')})"
    )

    return {
        'status': 'PASS',
        'data': {'subscription': result},
        'has_subscription': True,
        'plan_type': result.get('plan_type'),
        'is_cancelled': result.get('cancel_at_period_end', False)
    }


def test_cancel_subscription(client: BillingManagementClient, config: Config) -> Dict[str, Any]:
    """Test canceling a subscription (sets cancel_at_period_end=True)."""
    logger.info("Canceling subscription")

    # Get current state
    subscription_before = client.get_subscription(config.enterprise_customer_uuid)
    assert subscription_before is not None, "No subscription found to cancel"
    assert not subscription_before.get('cancel_at_period_end'), \
        "Subscription is already scheduled for cancellation"

    # Cancel subscription
    result = client.cancel_subscription(config.enterprise_customer_uuid)

    # Verify cancellation
    assert result['cancel_at_period_end'] is True, \
        "cancel_at_period_end should be True after cancellation"
    assert result['id'] == subscription_before['id'], "Subscription ID should not change"

    logger.info(f"Subscription cancelled: {result['plan_type']} - will end at period end")

    return {
        'status': 'PASS',
        'data': {'before': subscription_before, 'after': result},
        'cancellation_verified': True
    }


def test_reinstate_subscription(client: BillingManagementClient, config: Config) -> Dict[str, Any]:
    """Test reinstating a cancelled subscription (sets cancel_at_period_end=False)."""
    logger.info("Reinstating subscription")

    # Get current state
    subscription_before = client.get_subscription(config.enterprise_customer_uuid)
    assert subscription_before is not None, "No subscription found"
    assert subscription_before.get('cancel_at_period_end'), \
        "Subscription must be scheduled for cancellation to reinstate"

    # Reinstate subscription
    result = client.reinstate_subscription(config.enterprise_customer_uuid)

    # Verify reinstatement
    assert result['cancel_at_period_end'] is False, \
        "cancel_at_period_end should be False after reinstatement"
    assert result['id'] == subscription_before['id'], "Subscription ID should not change"

    logger.info(f"Subscription reinstated: {result['plan_type']} - active")

    return {
        'status': 'PASS',
        'data': {'before': subscription_before, 'after': result},
        'reinstatement_verified': True
    }


def test_cancel_and_reinstate_flow(client: BillingManagementClient, config: Config) -> Dict[str, Any]:
    """Test full cancel and reinstate workflow with state verification."""
    logger.info("Testing cancel and reinstate workflow")

    # Step 1: Get original state
    original = client.get_subscription(config.enterprise_customer_uuid)
    assert original is not None, "No subscription found"
    logger.info(f"Original: cancel_at_period_end={original.get('cancel_at_period_end')}")

    # Step 2: Cancel subscription
    logger.info("Step 1: Canceling subscription")
    cancelled = client.cancel_subscription(config.enterprise_customer_uuid)
    assert cancelled['cancel_at_period_end'] is True, "Cancellation failed"
    logger.info("✓ Subscription cancelled")

    # Step 3: Verify GET reflects cancellation
    logger.info("Step 2: Verifying GET reflects cancellation")
    after_cancel = client.get_subscription(config.enterprise_customer_uuid)
    assert after_cancel['cancel_at_period_end'] is True, "GET should reflect cancelled state"
    logger.info("✓ GET reflects cancelled state")

    # Step 4: Reinstate subscription
    logger.info("Step 3: Reinstating subscription")
    reinstated = client.reinstate_subscription(config.enterprise_customer_uuid)
    assert reinstated['cancel_at_period_end'] is False, "Reinstatement failed"
    logger.info("✓ Subscription reinstated")

    # Step 5: Verify GET reflects reinstatement
    logger.info("Step 4: Verifying GET reflects reinstatement")
    after_reinstate = client.get_subscription(config.enterprise_customer_uuid)
    assert after_reinstate['cancel_at_period_end'] is False, \
        "GET should reflect reinstated state"
    logger.info("✓ GET reflects reinstated state")

    return {
        'status': 'PASS',
        'data': {
            'original': original,
            'cancelled': cancelled,
            'after_cancel': after_cancel,
            'reinstated': reinstated,
            'after_reinstate': after_reinstate,
        },
        'workflow_verified': True
    }


def test_get_all_transactions(
    client: BillingManagementClient,
    config: Config
) -> Dict[str, Any]:
    """Test retrieving all transactions across pages."""
    logger.info("Retrieving all transactions (with pagination)")
    all_transactions = client.get_all_transactions(config.enterprise_customer_uuid)

    logger.info(f"Retrieved {len(all_transactions)} total transactions")

    # Verify no duplicates
    transaction_ids = [t['id'] for t in all_transactions]
    unique_ids = set(transaction_ids)
    assert len(transaction_ids) == len(unique_ids), "Found duplicate transaction IDs"

    return {
        'status': 'PASS',
        'data': {'total_count': len(all_transactions)},
        'total_transactions': len(all_transactions)
    }


def test_update_and_restore_address(
    client: BillingManagementClient,
    config: Config
) -> Dict[str, Any]:
    """Test updating address, restoring original, and verify cache invalidation on both updates."""
    logger.info("Testing address update, restore, and cache invalidation")

    # Get original address (this will cache it)
    logger.info("Step 1: Getting original address")
    original = client.get_address(config.enterprise_customer_uuid)
    logger.info(f"Original address name: {original.get('name')}")

    # Update to test values
    logger.info("Step 2: Updating to test values")
    test_address = {
        'name': 'Temporary Test Name',
        'email': original['email'],  # Keep original email
        'phone': original.get('phone', ''),
        'country': original.get('country', 'US'),
        'address_line_1': 'Temporary Test Address',
        'address_line_2': original.get('address_line_2', ''),
        'city': original.get('city', 'Test City'),
        'state': original.get('state', 'CA'),
        'postal_code': original.get('postal_code', '00000'),
    }

    updated = client.update_address(config.enterprise_customer_uuid, test_address)
    assert updated['name'] == test_address['name'], "Update failed"
    logger.info(f"Updated address name: {updated.get('name')}")

    # Get address to verify first cache invalidation
    logger.info("Step 3: Getting address after first update (verify cache invalidation #1)")
    refreshed_after_update = client.get_address(config.enterprise_customer_uuid)
    assert refreshed_after_update['name'] == test_address['name'], \
        f"Cache invalidation #1 failed! Expected '{test_address['name']}', got '{refreshed_after_update['name']}'"
    logger.info("✓ Cache invalidation verified after first update")

    # Restore original
    logger.info("Step 4: Restoring to original values")
    restored = client.update_address(config.enterprise_customer_uuid, original)
    assert restored['name'] == original['name'], "Restore failed"
    logger.info(f"Restored address name: {restored.get('name')}")

    # Get address to verify second cache invalidation
    logger.info("Step 5: Getting address after restore (verify cache invalidation #2)")
    refreshed_after_restore = client.get_address(config.enterprise_customer_uuid)
    assert refreshed_after_restore['name'] == original['name'], \
        f"Cache invalidation #2 failed! Expected '{original['name']}', got '{refreshed_after_restore['name']}'"
    logger.info("✓ Cache invalidation verified after restore")

    logger.info("✓ Both cache invalidations successful")

    return {
        'status': 'PASS',
        'data': {
            'original': original,
            'updated': updated,
            'refreshed_after_update': refreshed_after_update,
            'restored': restored,
            'refreshed_after_restore': refreshed_after_restore,
            'cache_invalidation_count': 2,
            'all_cache_invalidations_verified': True
        }
    }


# Test scenario registry
TEST_SCENARIOS = [
    TestScenario(
        name='health_check',
        description='Check API health endpoint',
        run=test_health_check,
        skip_on_error=False,
    ),
    TestScenario(
        name='get_address',
        description='Get billing address for enterprise',
        run=test_get_address,
        depends_on=['health_check'],
    ),
    TestScenario(
        name='update_address',
        description='Update billing address and verify cache invalidation',
        run=test_update_address,
        depends_on=['get_address'],
    ),
    TestScenario(
        name='list_payment_methods',
        description='List payment methods and verify status field (GAP-002)',
        run=test_list_payment_methods,
    ),
    TestScenario(
        name='attach_payment_method_endpoint',
        description='Test attach payment method endpoint exists and handles errors (GAP-001)',
        run=test_attach_payment_method_endpoint,
        depends_on=['health_check'],
    ),
    TestScenario(
        name='list_transactions',
        description='List transactions with default pagination',
        run=test_list_transactions,
    ),
    TestScenario(
        name='list_transactions_pagination',
        description='Test transaction pagination',
        run=test_list_transactions_pagination,
        depends_on=['list_transactions'],
        skip_on_error=True,  # May not have enough data for pagination
    ),
    TestScenario(
        name='get_subscription',
        description='Get subscription details',
        run=test_get_subscription,
    ),
    TestScenario(
        name='cancel_subscription',
        description='Cancel subscription (sets cancel_at_period_end=True)',
        run=test_cancel_subscription,
        depends_on=['get_subscription'],
        skip_on_error=True,
    ),
    TestScenario(
        name='reinstate_subscription',
        description='Reinstate cancelled subscription (sets cancel_at_period_end=False)',
        run=test_reinstate_subscription,
        depends_on=['cancel_subscription'],
        skip_on_error=True,
    ),
    TestScenario(
        name='cancel_and_reinstate_flow',
        description='Test full cancel and reinstate workflow with state verification',
        run=test_cancel_and_reinstate_flow,
        depends_on=['get_subscription'],
        skip_on_error=True,
    ),
    TestScenario(
        name='get_all_transactions',
        description='Get all transactions across pages',
        run=test_get_all_transactions,
        depends_on=['list_transactions'],
    ),
    TestScenario(
        name='update_and_restore_address',
        description='Update address, restore, and verify cache invalidation twice',
        run=test_update_and_restore_address,
        depends_on=['get_address', 'update_address'],
    ),
]


def get_scenario_by_name(name: str) -> Optional[TestScenario]:
    """Get a test scenario by name."""
    for scenario in TEST_SCENARIOS:
        if scenario.name == name:
            return scenario
    return None


def list_scenario_names() -> List[str]:
    """Get list of all scenario names."""
    return [s.name for s in TEST_SCENARIOS]
