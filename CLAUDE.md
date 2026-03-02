# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Enterprise Access is a Django-based microservice within the Open edX ecosystem that manages enterprise 
learner access to educational content through various subsidy mechanisms. The service handles policy
evaluation, subsidy redemptions, provisioning workflows, and integrations with related edX Enterprise services.

## Test and Quality Instructions

Assuming the app container is running, you can run unit tests and linters like this:
```bash
docker exec enterprise-access.app bash -c "DJANGO_SETTINGS_MODULE=enterprise_access.settings.test pytest -c pytest.local.ini enterprise_access/apps/api/v1/tests/test_customer_billing.py"
docker exec enterprise-access.app bash -c "make quality"
```
You can always find the running app container name with `docker ps | grep access`.

## Key Principles
- Search the codebase before assuming something isn't implemented
- Write comprehensive tests with clear documentation
- Follow Test-Driven Development when refactoring or modifying existing functionality
- Provide concise documentation for new functionality in the `docs/references` folder.
- Always write tests for new functionality you implement
- Make a note of when tests for some functionality have been completed. If you
  cannot run the tests, ask me to run them manually, then confirm whether they succeeded or failed.
- Keep changes focused and minimal
- Follow existing code patterns.

## Architecture Overview

This is a Django service for managing enterprise access to educational content, part of the Open edX ecosystem.
The `docs` folder contains documentation on a few specific features. `docs/architecture-overview.rst`
can be read when you need to understand the entire service beyond what's written below.

Always read `docs/architecture-patterns.md` before starting.

Check if a reusable code pattern exists in `.ralph/specs/stdlib` before executing code changes (ignore if this
directory does not exist).

### Core Applications

- **api** - Main API endpoints with versioned views (v1/), serializers, filters
- **subsidy_access_policy** - Core domain logic for access policies, subsidies, and redemptions
- **content_assignments** - Assignment-based access policies for learners
- **subsidy_request** - Learner credit request workflows and approval processes
- **bffs** (Backend for Frontend) - API aggregation layer, includes checkout BFF
- **customer_billing** - Stripe integration, checkout intents, pricing API
- **provisioning** - Enterprise provisioning workflows using abstract workflow pattern
- **workflow** - Abstract workflow framework for multi-step processes

### Key Concepts

- **Access Policies**: Define how learners can access content (per-learner enrollment caps, assigned credit)
- **Assignments**: Content assigned to specific learners through policies
- **Subsidy Requests**: Learner-initiated requests for access that require approval
- **BFF Pattern**: Backend aggregation for frontend applications (learner portal, admin)
- **Event-Driven**: Integrates with openedx-events via Kafka for cross-service communication

### External Service Integration

- **Enterprise Catalog**: Content metadata and discovery
- **Enterprise Subsidy**: Subsidy and transaction management  
- **LMS**: User management and course enrollment
- **Stripe**: Payment processing for self-service purchases
- **Braze**: Email and notification sending

### Docker Development

- Server runs on `localhost:18270`
- Uses MySQL 8.0, Memcache, Redis, and Celery worker
- Event bus via Kafka (Confluent Control Center at localhost:9021)

## Testing Notes

- Uses pytest with Django integration
- Coverage reporting enabled by default
- PII annotation checks required for Django models
- Separate test environments via tox for different Django versions

## Consolidate Patterns

If you discover a **reusable pattern** that future iterations should know, add it as a new
pattern in the `docs/architecture-patterns.md` file.

**Do NOT add:**
- Story-specific implementation details
- Temporary debugging notes
