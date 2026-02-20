# Ralph Agent Configuration

## Build Instructions

Assume the dev containers are already running.

## Test and Quality Instructions

You must use a docker container shell to run tests and linters.
```bash
# Run tests via docker container
docker run --rm edxops/enterprise-access-dev:latest bash -c "DJANGO_SETTINGS_MODULE=enterprise_access.settings.test pytest -c pytest.local.ini enterprise_access/apps/.../test_models.py::TestClass::test_method"
docker run --rm edxops/enterprise-access-dev:latest bash -c "DJANGO_SETTINGS_MODULE=enterprise_access.settings.test make quality"
```

## Notes
- Update this file when build process changes
- Add environment setup instructions as needed
- Include any pre-requisites or dependencies
- Line length: 120 characters
- Uses Django with pytest for testing
- Celery for background tasks
