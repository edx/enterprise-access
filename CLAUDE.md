# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Enterprise Access is a Django-based microservice within the Open edX ecosystem that manages enterprise 
learner access to educational content through various subsidy mechanisms. The service handles policy
evaluation, subsidy redemptions, provisioning workflows, and integrations with related edX Enterprise services.

## Test and Quality Instructions

- To run unit tests or generate coverage reports invoke the unit-tests skill.
- To run quality tests, invoke the quality-tests skill.

## Code Navigation

- Prefer using the LSP tool over grep/glob when navigating Python code (definitions, references, type info)

## Key Principles

- Search the codebase before assuming something isn't implemented
- Write comprehensive tests with clear documentation
- Follow Test-Driven Development when refactoring or modifying existing functionality
- Always write tests for new functionality you implement
- Keep changes focused and minimal
- Follow existing code patterns
- Prefer the `ddt` package for parameterized tests to reduce code duplication

## Documentation & Institutional Memory

- Document new functionality in `docs/references/`
- When you learn something important about how this codebase works (gotchas, non-obvious
  patterns, integration quirks), capture it in the relevant `docs/references/` file or
  in `docs/architecture-patterns.md`
- These docs are institutional memory - future sessions (yours or others) will benefit
  from what you record here

## Architecture Overview

This is a Django service for managing enterprise access to educational content, part of the Open edX ecosystem.
The `docs` folder contains documentation on a few specific features. `docs/architecture-overview.rst`
can be read when you need to understand the entire service beyond what's written below.

Always read `docs/architecture-patterns.md` before starting.

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

- Full devstack (app, worker, DB, Kafka, etc.) is managed in the devstack repository
- The container name is typically `edx.devstack.enterprise-access`,
  you can also do `docker ps | grep enterprise-access` to search for the container name.

## Testing Notes

- Uses pytest with Django integration
- Coverage reporting enabled by default
- PII annotation checks required for Django models

## Consolidate Patterns

If you discover a **reusable pattern** that future iterations should know, add it as a new
pattern in the `docs/architecture-patterns.md` file.

**Do NOT add:**
- Story-specific implementation details
- Temporary debugging notes

## Before opening a PR or pushing a branch

Run a self-check on the diff before creating a PR or pushing:
1. Confirm the diff is one coherent change — a single purpose a reviewer can hold in their head.
2. Look for logical seams: independently shippable or reviewable concerns bundled together (a refactor mixed with a feature, unrelated fixes, a schema migration plus unrelated UI work). If you find them, stop and propose a split along those seams before proceeding.
3. Size alone is never a reason to split. Note effective LoC and touched files (excluding lockfiles, generated files, snapshots, vendor code, and one-to-one test pairs) so reviewers can plan time; for a large but cohesive diff, suggest a review order or commit-by-commit reading.
4. Report the result inline before continuing.