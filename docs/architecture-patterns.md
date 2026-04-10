## Reusable Patterns
A running list of architectural patterns we like, in terms of both code design
and system design.

### 1. Role-Based Access Control (edx-rbac)
Uses `edx-rbac` for fine-grained permissions with:
- **System roles** (e.g., `SYSTEM_ENTERPRISE_ADMIN_ROLE`, `SYSTEM_ENTERPRISE_OPERATOR_ROLE`)
- **Feature roles** (e.g., `CONTENT_ASSIGNMENTS_ADMIN_ROLE`, `SUBSIDY_ACCESS_POLICY_LEARNER_ROLE`)
- **Implicit access** via JWT claims and **explicit access** via database-assigned roles
- `@rules.predicate` decorators to define access rules
- Viewsets inherit `PermissionRequiredMixin` to enforce permissions

### 2. DRF Spectacular for API Documentation
- REST APIs use DRF Spectacular (`drf_spectacular`) for OpenAPI schema generation
- Separate **request** and **response** serializers for clear contracts
- `@extend_schema` decorators on viewset methods define explicit request/response types
- `inline_serializer` for error responses (401, 403, 404, 429)

### 3. Django REST Framework Patterns
- **ViewSet inheritance hierarchy**: `ModelViewSet` → `PermissionRequiredMixin` + `UserDetailsFromJwtMixin` → Domain-specific viewsets
- **Dynamic serializer selection** via `get_serializer_class()` method (different serializers for create vs. update vs. read)
- **Custom `@action` decorators** for domain-specific endpoints (e.g., `redeem`, `allocate`, `acknowledge_assignments`)
- **Pagination** with custom `PaginationWithPageCount` class
- **Filtering** using `django-filters` and custom filter backends

### 4. Celery for Asynchronous Tasks
- Celery tasks for background processing (emails, provisioning, data sync)
- Custom `LoggedTaskWithRetry` base class with automatic retry logic for common exceptions
- Task-specific error handling with state tracking via `django-celery-results`
- Idempotent task design with task result throttling to prevent duplicate execution

### 5. Single-Table Inheritance for Policies
- Custom implementation using a discriminator field (`access_method` or `policy_type`)
- Proxy models for different policy types (e.g., `PerLearnerEnrollmentCreditAccessPolicy`)
- Custom manager (`PolicyManager`) to automatically "cast" records to correct proxy model
- Encapsulates business logic specific to each policy type

### 6. Abstract Workflow Pattern (attrs + Django Models)
- Generic, reusable framework for multi-step processes (e.g., provisioning)
- Uses **`attrs`/`cattrs`** for structured data classes (workflow inputs/outputs)
- Django models (`AbstractWorkflow`, `AbstractWorkflowStep`) for persistence
- Steps execute sequentially, each producing output consumed by next step
- Idempotent core functions allow safe re-execution

### 7. BFF (Backend for Frontend) Pattern
- Aggregates multiple service calls into single endpoint
- **Context** → **Handler** → **ResponseBuilder** architecture
- Context stores request data, Handler implements business logic, ResponseBuilder formats output
- Reduces frontend complexity and API round-trips

### 8. Service Client Pattern
- Dedicated client classes for external services (`LmsApiClient`, `EnterpriseCatalogApiClient`, `LicenseManagerApiClient`, `BrazeApiClient`)
- Base classes: `BaseOAuthClient` (service-to-service) and `BaseUserApiClient` (user-context forwarding)
- Centralized error handling and logging
- `@backoff` decorators for automatic retry on transient failures

### 9. Model Patterns
- **`TimeStampedModel`** (from `django-extensions`) for automatic `created`/`modified` timestamps
- **`simple_history`** for model change tracking (audit trail)
- **`SoftDeletableModel`** for soft deletion (sets `is_removed` flag instead of deleting)
- **PII annotations** required on all models (`# no_pii` comment)

### 10. Factory Pattern for Testing
- **Factory Boy** (`factory.django.DjangoModelFactory`) for test data generation
- Faker library for realistic test data
- Factories support `LazyAttribute`, `SubFactory`, `LazyFunction` for dynamic values
- `manufacture_data` management command to generate test data via CLI

### 11. attrs/cattrs vs DRF Serializers
- **DRF Serializers**: API request/response validation and JSON transformation
- **attrs/cattrs**: Internal workflow data structures and business logic layer
- Clear separation: DRF at API boundary, attrs for internal typed data

### 12. Configuration Pattern
- Settings split by environment (`base.py`, `local.py`, `devstack.py`, `test.py`)
- Environment variables for sensitive config
- `SYSTEM_TO_FEATURE_ROLE_MAPPING` for role hierarchy

### 13. Caching Strategy
- Tiered caching (`edx-django-utils.cache.TieredCache`)
- Different timeouts for different data types (5 min default, 30 min for content metadata)
- Explicit cache invalidation on writes

### 14. Validation Pattern
- **Field-level** validation (format, length, regex)
- **Cross-field** validation (business rules across multiple fields)
- **Pre-write** validation in serializers before database operations
- Validation responses with structured error codes and developer messages

### 15. Use ddt to parameterize unit tests
- Improve test DRYness by using the `ddt` packages `@data` and `@unpack` decorators.

### 16. Module Splitting with Re-exports
When files grow too large (>500-1000 lines), split them while maintaining backwards compatibility:

**Pattern:**
1. Create new submodule(s) with extracted code
2. Re-export from original module so existing imports continue to work
3. Add pylint/noqa comments to suppress false-positive warnings on re-exports

**Example - handlers.py (avoiding circular imports):**
```python
# bffs/base.py - base class with no handler dependencies
class BaseHandler:
    ...

# bffs/learner_portal/handlers.py - imports from base.py (not handlers.py)
from enterprise_access.apps.bffs.base import BaseHandler

class BaseLearnerPortalHandler(BaseHandler):
    ...

# bffs/handlers.py - pure re-exports for backwards compatibility
from enterprise_access.apps.bffs.base import BaseHandler
from enterprise_access.apps.bffs.learner_portal.handlers import (
    BaseLearnerPortalHandler,
    DashboardHandler,
)
```

**Example - models.py:**
```python
# models.py - keeps core models, re-exports supporting models
class SubsidyAccessPolicy:
    ...

# Re-export for backwards compatibility
# pylint: disable=wrong-import-position,unused-import
from .models_supporting import (  # noqa: E402,F401
    PolicyGroupAssociation,
    ForcedPolicyRedemption,
)
```

**When to use:**
- Files exceed ~1000 lines
- Clear domain boundaries exist for extraction
- ForeignKey relationships use string references (`'app.Model'`) to avoid circular imports
- Base classes go in separate `base.py` to prevent circular imports when child classes are in submodules

**Benefits:**
- No changes required to existing import statements
- Reduced cognitive load per file
- Easier testing and maintenance

### Key Takeaways for Implementation:
- Check permissions early using `@permission_required` decorator
- Use separate serializers for request/response
- Background tasks should be idempotent and retryable
- Leverage `attrs` for internal data structures, DRF serializers for API contracts
- All database writes should have corresponding history tracking
- Use factory pattern for all test data creation
