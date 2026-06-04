# Billing Management API Integration Tests

Integration tests for the billing management API endpoints using real OAuth authentication and Stripe sandbox data.

## Overview

This test suite provides comprehensive integration testing for the billing management API, including:

- Billing address management
- Payment method operations
- Transaction/invoice listing with pagination
- Subscription management
- OAuth JWT authentication

## Quick Start

### 1. Install Dependencies

The test script requires the `requests` library and `python-dotenv`:

```bash
cd scripts/
pip install -r requirements.txt
```

### 2. Configure Credentials

Copy the example environment file and fill in your credentials:

```bash
cd scripts
cp .env.example .env
```

Edit `.env` and provide:
- OAuth client ID and secret (from Django admin)
- API base URL (typically `http://localhost:18270/api/v1`)
- Enterprise customer UUID for testing
- (Optional) Stripe API key and customer ID

**Important:** Never commit the `.env` file! It's already in `.gitignore`.

### 3. Run Tests

Run all tests:

```bash
python scripts/test_billing_integration.py --all
```

Run specific test:

```bash
python scripts/test_billing_integration.py --test get_address
```

Run with verbose output:

```bash
python scripts/test_billing_integration.py --all --verbose
```

## Usage

### List Available Tests

```bash
python scripts/test_billing_integration.py --list
```

### Run Specific Tests

Run one test:
```bash
python scripts/test_billing_integration.py --test health_check
```

Run multiple tests:
```bash
python scripts/test_billing_integration.py --test get_address --test update_address
```

### Advanced Options

Enable debug logging:
```bash
python scripts/test_billing_integration.py --all --debug
```

Use custom environment file:
```bash
# run from project root, say...
python scripts/test_billing_integration.py --all --env=scripts/.env
```

Stop on first failure:
```bash
python scripts/test_billing_integration.py --all --stop-on-fail
```

## Available Test Scenarios

| Test Name | Description |
|-----------|-------------|
| `health_check` | Check API health endpoint |
| `get_address` | Get billing address for enterprise |
| `update_address` | Update billing address and verify cache invalidation |
| `list_payment_methods` | List payment methods |
| `list_transactions` | List transactions with default pagination |
| `list_transactions_pagination` | Test transaction pagination |
| `get_subscription` | Get subscription details |
| `get_all_transactions` | Get all transactions across pages |
| `update_and_restore_address` | Update address, restore, and verify cache invalidation twice |

### Cache Invalidation Testing

The `update_address` and `update_and_restore_address` tests specifically verify that cache invalidation works correctly:

- **`update_address`**:
  1. Gets the current address (caches it)
  2. Updates the address with new data
  3. Gets the address again and verifies it returns the updated data (not stale cache)

- **`update_and_restore_address`**:
  1. Gets the original address (caches it)
  2. Updates to test values
  3. Gets the address again and verifies cache was invalidated (test #1)
  4. Restores to original values
  5. Gets the address again and verifies cache was invalidated (test #2)

## Project Structure

```
scripts/
├── test_billing_integration.py    # Main test script
├── billing_integration/
│   ├── __init__.py               # Package init
│   ├── auth.py                   # JWT authentication
│   ├── client.py                 # API client wrapper
│   ├── config.py                 # Configuration loading
│   └── test_scenarios.py         # Test definitions
├── .env.example                  # Example configuration
└── README.md                     # This file
```

## Configuration

### Required Environment Variables

```bash
# OAuth JWT Authentication
OAUTH_CLIENT_ID=your_client_id
OAUTH_CLIENT_SECRET=your_client_secret
OAUTH_TOKEN_URL=http://localhost:18000/oauth2/access_token

# API Configuration
API_BASE_URL=http://localhost:18270/api/v1
ENTERPRISE_CUSTOMER_UUID=your-enterprise-uuid
```

### Optional Environment Variables

```bash
# Stripe (for verification/debugging)
STRIPE_API_KEY=sk_test_...
STRIPE_CUSTOMER_ID=cus_...

# Test user credentials
TEST_USER_EMAIL=test@example.com
TEST_USER_PASSWORD=password
```

## Getting OAuth Credentials

1. Access Django admin at `http://localhost:18000/admin/`
2. Navigate to **Django OAuth Toolkit** → **Applications**
3. Click **Add Application**
4. Configure:
   - **Client type**: Confidential
   - **Authorization grant type**: Client credentials
   - **Name**: Billing Integration Tests (or any name)
5. Save and copy the generated **Client ID** and **Client secret**
6. Add these to your `.env` file

## Finding Your Enterprise UUID

You can find the enterprise customer UUID in several ways:

1. **Django Admin**: Go to **Enterprise** → **Enterprise Customers**
2. **API**: Use the enterprise customer list endpoint
3. **Stripe**: Check customer metadata in Stripe dashboard

## Test Dependencies

Some tests depend on others passing first. The script automatically resolves dependencies. For example:

- `update_address` depends on `get_address`
- `list_transactions_pagination` depends on `list_transactions`
- `get_all_transactions` depends on `list_transactions`

## Writing New Tests

To add a new test scenario:

1. Open `billing_integration/test_scenarios.py`
2. Write your test function:

```python
def test_my_feature(client: BillingManagementClient, config: Config) -> Dict[str, Any]:
    """Test description."""
    result = client.some_endpoint(config.enterprise_customer_uuid)

    # Add assertions
    assert 'field' in result

    return {'status': 'PASS', 'data': result}
```

3. Register it in `TEST_SCENARIOS`:

```python
TestScenario(
    name='my_feature',
    description='Test my new feature',
    run=test_my_feature,
    depends_on=['health_check'],  # Optional dependencies
)
```

## Troubleshooting

### Authentication Errors

**Problem**: `Authentication failed: 401 Unauthorized`

**Solutions**:
- Verify OAuth credentials in `.env`
- Ensure OAuth application is configured correctly in Django admin
- Check that `OAUTH_TOKEN_URL` is correct and accessible

### Configuration Errors

**Problem**: `Missing required environment variables`

**Solutions**:
- Ensure `.env` file exists in `scripts/` directory
- Check that all required variables are set
- Verify `.env` file is readable

### Connection Errors

**Problem**: `Connection refused` or `Connection timeout`

**Solutions**:
- Ensure the API server is running via devstack
- Verify `API_BASE_URL` matches your server address
- Check that port 18270 is accessible

### Test Failures

**Problem**: Tests fail with 404 or similar errors

**Solutions**:
- Verify the enterprise UUID exists and has billing configured
- Ensure Stripe customer is properly linked
- Check that you have proper permissions for the enterprise
- Run with `--debug` flag for detailed logs

## Security Best Practices

1. **Never commit `.env` files** - Already in `.gitignore`
2. **Use test/sandbox credentials only** - Never use production Stripe keys

## CI/CD Integration

To run these tests in CI/CD:

```bash
# Example: GitHub Actions
- name: Run Integration Tests
  env:
    OAUTH_CLIENT_ID: ${{ secrets.OAUTH_CLIENT_ID }}
    OAUTH_CLIENT_SECRET: ${{ secrets.OAUTH_CLIENT_SECRET }}
    API_BASE_URL: ${{ secrets.API_BASE_URL }}
    ENTERPRISE_CUSTOMER_UUID: ${{ secrets.ENTERPRISE_UUID }}
  run: |
    python scripts/test_billing_integration.py --all
```

## Support

For issues or questions:

1. Check this README first
2. Review test logs with `--debug` flag
3. Check Django admin for configuration issues
4. Verify Stripe sandbox data
5. Open an issue in the project repository

## License

Same as parent project.
