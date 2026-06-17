---
name: unit-tests
description: Run Django unit tests in the enterprise-access Docker container. Use when the user wants to run tests, check if tests pass, or verify test coverage.
argument-hint: "<TEST_FILES>"
allowed-tools: Bash(docker *), Bash(make *), Bash(colima *)
---

## Arguments

`<TEST_FILES>` (optional): One or more folders, test file paths, or pytest node IDs to run. Coverage is only enabled when a single folder representing a code domain is provided or no arguments are provided.

Examples:
- `/unit-tests` — run all tests with coverage
- `/unit-tests enterprise_access/apps/bffs` — run all bff domain tests with domain-only coverage
- `/unit-tests enterprise_access/apps/customer_billing/tests/test_models.py` — run one test file without coverage
- `/unit-tests enterprise_access/apps/subsidy_access_policy/tests/test_models.py enterprise_access/apps/content_assignments/tests/test_api.py` — run multiple test files without coverage
- `/unit-tests enterprise_access/apps/customer_billing/tests/test_models.py::TestSomeClass::test_method` — run a single test by node ID without coverage

## Routing rules (evaluate in order)

- **No arguments** → Step 2c (whole-project tests + whole-project coverage)
- **Single argument that is a directory** (no `.py`, no `::`) → Step 2b (domain tests + domain coverage). Never enable coverage when testing specific files — only for domain or whole-project runs.
- **Everything else** (`.py` files, node IDs, multiple args) → Step 2a (targeted tests, NO coverage)

## Steps

### 1. Make sure the app container is running

Determine if the app container is running:

```bash
docker compose ps
```

Start the app container if not running:

```bash
make dev.up
```

### 2a. Run specific unit test files or functions

If `<TEST_FILES>` specify .py files or functions/classes within, run only those tests using `pytest.local.ini` to disable coverage and warnings:

```bash
docker compose exec app bash -c "pytest -c pytest.local.ini <TEST_FILES>"
```

Example `<TEST_FILES>` which match this case:
- enterprise_access/apps/customer_billing/tests/test_models.py
- enterprise_access/apps/subsidy_access_policy/tests/test_models.py enterprise_access/apps/content_assignments/tests/test_api.py
- enterprise_access/apps/customer_billing/tests/test_models.py::TestSomeClass::test_method

Never enable coverage reports (by adding `--cov`) when only testing specific files, since the results will be misleading.

### 2b. Run domain unit tests and generate domain-only coverage

If `<TEST_FILES>` specifies a single directory which represents a domain, such as a django app, enable coverage.

Convert the directory path to a Python module path for `--cov`: replace `/` with `.` and strip any trailing slash. Example: `enterprise_access/apps/bffs/` → `enterprise_access.apps.bffs`

```bash
docker compose exec app bash -c "pytest -c pytest.local.ini <TEST_DOMAIN> --cov=<MODULE_PATH_OF_TEST_DOMAIN>"

# Example: <TEST_FILES> is "enterprise_access/apps/bffs"
docker compose exec app bash -c "pytest -c pytest.local.ini enterprise_access/apps/bffs --cov=enterprise_access.apps.bffs"
```

Example `<TEST_FILES>` which match this case:
- enterprise_access/apps/api
- enterprise_access/apps/api_client/
- enterprise_access/apps/bffs
- enterprise_access/apps/content_assignments/
- enterprise_access/apps/customer_billing/

Whole-domain coverage will be reported in the console output. Specific line numbers with missing coverage will be reported.

### 2c. Run whole-project unit tests and generate coverage

If no arguments are given, assume the user wants to run the full test suite for the entire project:

```bash
docker compose exec app bash -c "make test"
```

Whole-project coverage will be reported in the console output. Specific line numbers with missing coverage will be reported.

## Troubleshooting

### ModuleNotFoundError

If tests fail due to missing imports, first try to install requirements:

```bash
docker compose exec app make ci_requirements
```

This is necessary at least when adding new requirements which have not yet been built into the image.

### Failed to connect to the docker API on MacOS

This likely just means colima needs to be started:

```bash
colima start
```
