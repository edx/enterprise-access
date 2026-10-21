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

### 16. Evaluation harnesses own no domain logic
- An eval app (`apps/pathway_eval`) loads fixtures, calls production code, scores results, reports
- Pipeline logic — retrieval, translation, ranking — stays production code elsewhere; if the
  harness reimplements any of it, the harness is what gets measured
- Fixtures validate against the **real** request serializer, not a copy of its rules, so a
  contract change fails at load time rather than mid-run
- Ground-truth fixtures declare their own provenance in a machine-readable field
  (`ground_truth_status: placeholder | expert_authored`). Authoring ground truth outlasts
  building the harness, so "is this a real expectation?" must be queryable — a comment in a
  fixture cannot keep a placeholder out of a headline metric
- Incomplete ground truth **loads** and reports itself as unscoreable. It is neither a
  validation error (it must be visible and chaseable) nor a scored failure
- Diagnostics name their epistemic limits in the result. Where a credential can only probe
  rather than enumerate, the outcome is `NOT_FOUND_IN_INDEX`, not "absent" — an over-claimed
  negative drives the most expensive decisions

### 17. Search clients bind a credential to an index, not to a service
- Where two indexes need different credentials, make the wrong pairing unconstructable rather
  than validated: separate methods per index, and no parameter for the credential that must
  not be used there
- Guard against a *misconfigured* credential too, not just a miscalled one
- A degraded mode (unscoped search) requires both a settings flag and an explicit call-site
  argument, and is never reached by fallback — missing or expired scoping raises

### 18. Isolate per-subsidy failures when fanning out to enterprise-subsidy
A customer can own several subsidies, and one can become unreachable on its own while the others stay healthy.
Any loop that calls enterprise-subsidy once per subsidy should drop just the failing subsidy rather than abort,
so the learner still gets an answer for the healthy ones.

- **Know which status codes mean "this one subsidy".** A soft-deleted (or unknown) subsidy answers **403**, not
  404: enterprise-subsidy resolves the RBAC context through `Subsidy.objects`, which hides soft-deleted rows, so
  the permission check fails before the view runs. 404 only covers a subsidy that disappears after that check.
- **A 403 from every subsidy is not per-subsidy.** It looks the same as our own service credentials losing their
  role, so when *every* subsidy is unreachable, re-raise instead of degrading. Otherwise learners are quietly
  told nothing is available during what is really an outage. A 5xx always affects every subsidy and is always
  re-raised. See `UNREACHABLE_SUBSIDY_STATUS_CODES` in `subsidy_access_policy/subsidy_api.py`.
- **`can-redeem` fans out at two independent points.**
  `get_redemptions_by_content_and_policy_for_learner()` fetches the learner's transactions per subsidy, and
  `evaluate_policies()` separately calls `policy.can_redeem()` per policy, which calls the subsidy's `can_redeem`
  endpoint and doesn't catch `HTTPError`. `evaluate_policies()` re-reads `get_queryset()`, so the failing
  subsidy's policies must be excluded there too, or the second fan-out fails the request anyway.
- **`subsidy_record()` swallows `HTTPError` and returns `{}`,** so a dead subsidy's policy reports a null
  expiration and a zero balance rather than raising. Code that compares those fields across policies (for
  example `sort_subsidy_access_policies_for_redemption()`) must tolerate nulls.

### 19. Multi-write state transitions that also queue a Celery task: atomic writes, task on commit

When a job changes state, writes an audit row and then queues a Celery task (e.g. `expire_assignment()`), do the
writes inside `transaction.atomic()` and register the `.delay()` with `transaction.on_commit()`. Queuing inside the
block lets a fast worker run before the state is visible, and still sends the task if the commit then fails. The
trade-off is that a broker failure after the commit leaves the record in its final state with no task queued; the
exception surfaces to the caller (the nightly command reports it as a failure), but nothing retries it
automatically. The rollback only restores the database, so don't reuse a mutated in-memory model after the block
fails: load it again before retrying (the nightly job does this by reading fresh rows on its next run). In tests,
`TestCase` never runs `on_commit` callbacks on its own: wrap the call in `self.captureOnCommitCallbacks(execute=True)`.

### 20. Irreversible behavior ships behind a WaffleSwitch

PII clearing cannot be undone, so behavior that extends it (e.g. `COURSE_RUN_ENDED` expiration) is gated by a
switch in `enterprise_access/toggles.py`, off by default, and the switch must also stop PII clearing for rows
already recorded under that behavior. Use `override_waffle_switch` on the test class to turn it on.

### 21. Catalog lookups on scheduled jobs: only a 404 is "no metadata"

A transient catalog failure must not be treated as "no metadata", or the job acts on incomplete data and records a
reason that is never recomputed. Let non-404 `HTTPError`s propagate so the per-item error handling skips the item
and the next run retries it, and never cache a failure as a miss.

### 22. Don't offset-paginate a queryset the loop shrinks

Expiring a record removes it from the filter that selected it, so `Paginator` over the live queryset skips records.
Freeze the primary keys first (`list(qs.values_list('pk', flat=True))`) and page over that.

### 23. `bnr_enabled` means "has requests to manage", not "can take new requests"

A policy's `bnr_enabled` (its `LearnerCreditRequestConfiguration` is active) stays True after the budget is
retired or its subsidy expires, so admins can still open the budget's Requests tab and decline what is pending.
Anything that decides whether a learner may *submit* a request must also require an enabled policy with a live
subsidy: use `get_policies_accepting_requests()` in `subsidy_access_policy/subsidy_api.py`, which `can-request`
and request creation share. Approval enforces the same rule separately in `can_allocate_all`. The BFF's
`has_bnr_enabled_policy` still gates on `bnr_enabled` alone.

### Key Takeaways for Implementation:
- Check permissions early using `@permission_required` decorator
- Use separate serializers for request/response
- Background tasks should be idempotent and retryable
- Leverage `attrs` for internal data structures, DRF serializers for API contracts
- All database writes should have corresponding history tracking
- Use factory pattern for all test data creation
