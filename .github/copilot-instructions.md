# GitHub Copilot Instructions for `enterprise-access`

## Service Overview

**Enterprise Access** is a Django-based microservice within the Open edX ecosystem. It manages enterprise
learner access to educational content through subsidy mechanisms: access policies, content assignments,
learner-credit requests, billing (Stripe), and provisioning workflows.

- **Dev server**: `http://localhost:18270`
- **Django version**: 4.2 and 5.2 (both tested in CI) | **Python**: 3.12
- **Primary database**: MySQL 8.0 (SQLite in tests)
- **Cache**: Redis + Memcached (tiered)
- **Async tasks**: Celery with Redis broker
- **Events**: Kafka via `openedx-events` / `edx-event-bus-kafka`
- **Auth**: OAuth2 + JWT + RBAC (`edx-rbac`)
- **Payments**: Stripe

Detailed architecture: `docs/architecture-overview.rst`
Reusable patterns: `docs/architecture-patterns.md`

---

## Repository Layout

```
enterprise_access/
├── apps/
│   ├── api/                    # Versioned REST API endpoints (v1/)
│   ├── subsidy_access_policy/  # Core learner credit domain logic & redemptions
│   ├── content_assignments/    # Assignment-based access policies
│   ├── subsidy_request/        # Request/approval workflows
│   ├── customer_billing/       # Stripe integration & self-service purchasing
│   ├── bffs/                   # Backend-for-Frontend aggregation layer
│   ├── provisioning/           # Enterprise provisioning workflows
│   ├── workflow/               # Abstract multi-step workflow framework
│   ├── api_client/             # Clients for LMS, Catalog, Subsidy, Braze, Stripe
│   ├── events/                 # Kafka event handling & publishing
│   ├── track/                  # Segment analytics tracking
│   ├── core/                   # Shared models (User), utilities, constants
│   ├── content_metadata/       # Content pricing & metadata caching
│   ├── enterprise_groups/      # Enterprise group management
│   └── admin_portal_learner_profile/  # Admin portal data aggregation
├── docs/                       # Architecture docs, decision records, guides
├── requirements/               # Pinned dependency files (base, test, dev, quality)
├── test_utils/                 # Shared test fixtures and utilities
└── scripts/                    # Billing integration helpers & test-data generators
```

---

## Development Environment

### Docker-based (recommended)

```bash
make docker_build    # Build images
make dev.provision   # First-time setup (creates DB, runs migrations)
make dev.up          # Start all services (app on :18270, MySQL, Memcache, Celery)
make app-shell       # Open a shell in the running app container
make migrate         # Apply database migrations
```

### Without Docker

```bash
make requirements          # Install all dev requirements
./manage.py migrate        # Apply migrations
./manage.py runserver 18270
```

---

## Running Tests

Tests use **pytest-django**. Always use `pytest.local.ini` for local runs (no coverage overhead):

```bash
# Inside the running container (preferred):
docker exec enterprise-access.app bash -c \
  "DJANGO_SETTINGS_MODULE=enterprise_access.settings.test \
   pytest -c pytest.local.ini enterprise_access/apps/<app>/tests/"

# Or via Makefile (runs full suite with coverage):
make test

# Run a specific test file:
docker exec enterprise-access.app bash -c \
  "DJANGO_SETTINGS_MODULE=enterprise_access.settings.test \
   pytest -c pytest.local.ini enterprise_access/apps/api/v1/tests/test_customer_billing.py"

# Multiple Django versions via tox:
tox -e py312-django52
```

**Test settings**: `enterprise_access.settings.test`
- In-memory SQLite database
- Celery runs eagerly (synchronous)
- External services are mocked

---

## Code Quality

```bash
make quality           # All quality checks: style + isort + lint + pii_check
make style             # pycodestyle (max line length 120)
make isort             # Sort imports (fix)
make isort_check       # Sort imports (check only)
make lint              # pylint (currently scoped to customer_billing)
make pii_check         # Verify PII annotations on all models
make validate          # Tests + quality + PII (what CI runs)
```

Line length is **120** characters. Import order is enforced by `isort`.

---

## Key Architectural Patterns

### 1. Role-Based Access Control

Uses `edx-rbac`. ViewSets inherit `PermissionRequiredMixin`. Roles are:
- **System roles**: `SYSTEM_ENTERPRISE_ADMIN_ROLE`, `SYSTEM_ENTERPRISE_OPERATOR_ROLE`
- **Feature roles**: `CONTENT_ASSIGNMENTS_ADMIN_ROLE`, `SUBSIDY_ACCESS_POLICY_LEARNER_ROLE`, etc.

Access rules are defined with `@rules.predicate` and the role mapping lives in
`SYSTEM_TO_FEATURE_ROLE_MAPPING` in settings.

### 2. DRF + drf-spectacular

- Separate **request** and **response** serializers.
- Use `@extend_schema` on viewset actions for OpenAPI documentation.
- Use `inline_serializer` for error responses (401, 403, 404, 429).
- Custom pagination: `PaginationWithPageCount`.
- API docs: `/api-docs/` (Swagger) and `/api/schema/redoc/`.

### 3. BFF (Backend-for-Frontend) Pattern

**Context → Handler → ResponseBuilder** pipeline. Each BFF endpoint aggregates
multiple upstream service calls into a single response tailored for a specific frontend.

Implementations:
- `bffs/learner/` — Learner portal (policy info, assignments, spend data)
- `bffs/checkout/` — Checkout flow (pricing, Stripe session)

See `docs/checkout_bff.rst` for a worked example.

### 4. Single-Table Inheritance for Policies

`SubsidyAccessPolicy` uses a discriminator field (`policy_type`). Proxy models such as
`PerLearnerEnrollmentCreditAccessPolicy` encapsulate type-specific business logic.
A custom `PolicyManager` auto-casts database rows to the correct proxy model.

### 5. Abstract Workflow Pattern

`AbstractWorkflow` / `AbstractWorkflowStep` (in the `workflow` app) provide a generic
multi-step framework. Steps execute sequentially; each step's output is available to the
next. Uses `attrs`/`cattrs` for typed input/output data. Design steps to be idempotent.

### 6. Service Client Pattern

All external service communication goes through client classes in `api_client/`:
- `LmsApiClient` – user/enrollment data
- `EnterpriseCatalogApiClient` – content metadata
- `SubsidyApiClient` – subsidy/transaction management
- `BrazeApiClient` – notifications/email
- `StripeApiClient` – payments

Base classes: `BaseOAuthClient` (service-to-service) and `BaseUserApiClient`
(forward the user's auth token). Use `@backoff` for retry on transient errors.

### 7. Celery Tasks

Inherit from `LoggedTaskWithRetry` for automatic retry logic and logging.
Design tasks to be idempotent. Use `django-celery-results` to track task state.

### 8. Model Conventions

- Inherit from `TimeStampedModel` for `created`/`modified` timestamps.
- Use `simple_history` for audit trails on important models.
- Use `SoftDeletableModel` for soft-delete (`is_removed` flag).
- **Every model must have a PII annotation** (at minimum `# no_pii` comment directly
  above the class). CI enforces this via `make pii_check`.

### 9. Caching

Use `TieredCache` from `edx-django-utils`. Default TTL is 5 minutes; content metadata
uses 30 minutes. Always invalidate the cache explicitly on writes.

### 10. Testing Conventions

- **Factory Boy** (`factory.django.DjangoModelFactory`) for all test data — never
  create model instances manually in tests.
- **`ddt`** (`@data` / `@unpack`) to parametrize test scenarios (DRY tests).
- **`pytest` fixtures** in `conftest.py` for shared setup.
- Factories live in `<app>/tests/factories.py`.
- Mock all external service calls; do not make real HTTP requests in tests.

---

## URL Structure

```
/admin/                          Django admin
/api/v1/                         REST API (see below)
/api-docs/                       Swagger UI
/api/schema/redoc/               ReDoc
/health/                         Health check
```

Key API v1 routes (`enterprise_access/apps/api/v1/urls.py`):
| Route | ViewSet |
|---|---|
| `subsidy-access-policies/` | `SubsidyAccessPolicyViewSet` |
| `policy-redemption/` | `SubsidyAccessPolicyRedeemViewset` |
| `policy-allocation/` | `SubsidyAccessPolicyAllocateViewset` |
| `assignment-configurations/` | `AssignmentConfigurationViewSet` |
| `learner-credit-requests/` | `LearnerCreditRequestViewSet` |
| `customer-billing/` | `CustomerBillingViewSet` |
| `checkout-intent/` | `CheckoutIntentViewSet` |
| `bffs/learner/` | `LearnerPortalBFFViewSet` |
| `bffs/checkout/` | `CheckoutBFFViewSet` |
| `provisioning/` | `ProvisioningCreateView` |

---

## Settings & Configuration

Settings live in `enterprise_access/settings/`:

| File | Used by |
|---|---|
| `base.py` | All environments (base config) |
| `local.py` | Local development |
| `devstack.py` | Docker dev stack |
| `test.py` | `pytest` test runs |
| `production.py` | Production |

For tests, always use `DJANGO_SETTINGS_MODULE=enterprise_access.settings.test`.

---

## CI/CD

GitHub Actions workflow: `.github/workflows/ci.yml`

Matrix: Python 3.12 × Django 4.2 (pinned requirements) and Django 5.2

Pipeline steps:
1. Install requirements
2. Validate translations
3. `make test` (pytest with coverage)
4. Upload coverage to Codecov (pinned build only)
5. `make style isort_check pii_check check_keywords`

All of these steps must pass before a PR can be merged.

---

## Common Workflows

### Adding a new API endpoint

1. Create/update the model in the appropriate app.
2. Write a Factory and model tests.
3. Create serializers (separate request/response if needed).
4. Add a ViewSet inheriting from `PermissionRequiredMixin` + `UserDetailsFromJwtMixin`.
5. Register the ViewSet in `enterprise_access/apps/api/v1/urls.py`.
6. Annotate the ViewSet with `@extend_schema`.
7. Run `make quality` and `make test`.

### Adding a Celery task

1. Create the task in `<app>/tasks.py` inheriting `LoggedTaskWithRetry`.
2. Design it to be idempotent.
3. Register it in `CELERY_TASK_ROUTES` if routing is needed.
4. Write tests with `@override_settings(CELERY_TASK_ALWAYS_EAGER=True)`.

### Adding a new model

1. Inherit from `TimeStampedModel` (and `SoftDeletableModel` if soft-delete is needed).
2. Add a PII annotation comment immediately before the class definition.
3. Add `simple_history.register(MyModel)` if audit trails are required.
4. Write a Factory in `<app>/tests/factories.py`.
5. Run `make pii_check` to verify annotation coverage.
6. Generate and apply migrations.

---

## Known Issues & Workarounds

- **Docker container name**: The app container is named `enterprise-access.app`. Use
  `docker ps | grep access` to confirm the exact name if commands fail.
- **Kafka not required locally**: Event bus is optional for most development tasks.
  Set `KAFKA_ENABLED=False` in local settings to skip Kafka connectivity.
- **Stripe webhooks in dev**: Use `stripe listen --forward-to localhost:18270/api/v1/...`
  to forward Stripe events locally.
- **PII check failures**: Every Django model class must have a `# no_pii` annotation
  (or a proper PII annotation) directly above the class body. The check is run by
  `make pii_check` via `code-annotations`. Missing annotations fail CI.
- **pylint scope**: Currently `make lint` only runs pylint on `customer_billing`.
  Other apps may have latent pylint issues — check before expanding scope.
- **Import order**: Always run `make isort` after adding new imports, then confirm
  with `make isort_check`.

---

## Useful References

- `docs/architecture-patterns.md` — Canonical list of reusable code patterns
- `docs/architecture-overview.rst` — Full service architecture and domain model
- `docs/checkout_bff.rst` — BFF pattern walkthrough
- `docs/caching.rst` — Caching strategy details
- `docs/segment_events.rst` — Analytics event catalogue
- `docs/decisions/` — Architectural decision records
- `CLAUDE.md` — Additional guidance for AI coding assistants
