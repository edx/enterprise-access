# Ralph Agent Configuration

## Build Instructions

Assume the dev containers are already running.

## Test and Quality Instructions
Assuming the app container is running, you can run tests and linters like this:
```bash
docker compose exec app bash -c "DJANGO_SETTINGS_MODULE=enterprise_access.settings.test pytest -c pytest.local.ini enterprise_access/apps/api/v1/tests/test_customer_billing.py"
docker compose exec app bash -c "make quality"
```

## Notes
- Update this file when build process changes
- Add environment setup instructions as needed
- Include any pre-requisites or dependencies
- Line length: 120 characters
- Uses Django with pytest for testing
- Celery for background tasks
