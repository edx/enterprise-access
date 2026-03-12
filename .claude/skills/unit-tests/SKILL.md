---
name: unit-tests
description: Run Django unit tests in the enterprise-access Docker container. Use when the user wants to run tests, check if tests pass, or verify test coverage.
argument-hint: "<TEST_FILES>"
allowed-tools: Bash(docker *), Bash(make *)
---

## Arguments

`<TEST_FILES>` (optional): One or more test file paths or pytest node IDs to run. When provided, only the specified tests are run and coverage is disabled (`--no-cov`). When omitted, the full test suite runs with coverage enabled.

Examples:
- `/unit-tests` — run all tests with coverage
- `/unit-tests enterprise_access/apps/customer_billing/tests/test_models.py` — run one test file without coverage
- `/unit-tests enterprise_access/apps/subsidy_access_policy/tests/test_models.py enterprise_access/apps/content_assignments/tests/test_api.py` — run multiple test files without coverage
- `/unit-tests enterprise_access/apps/customer_billing/tests/test_models.py::TestSomeClass::test_method` — run a single test by node ID without coverage

## Steps

### 1. Make sure the app container is running

Determine if the enterprise-access.app container is running:

```bash
docker ps | grep enterprise-access.app
```

Start the app container if not running:

```bash
make dev.up
```

### 2. Run unit tests

If `<TEST_FILES>` argument(s) are given, run only those tests with coverage disabled:

```bash
docker exec enterprise-access.app bash -c "DJANGO_SETTINGS_MODULE=enterprise_access.settings.test pytest --no-cov <TEST_FILES>"
```

If no arguments are given, run the full test suite with coverage enabled:

```bash
docker exec enterprise-access.app bash -c "DJANGO_SETTINGS_MODULE=enterprise_access.settings.test pytest"
```
