# Enterprise Access Service - Architecture Overview

> **Note:** This document provides a comprehensive overview of the Enterprise Access service architecture for development teams new to the Open edX ecosystem.

## Introduction

Enterprise Access is a Django-based microservice within the Open edX ecosystem that manages enterprise
learner access to educational content through various subsidy mechanisms. The service handles:

- **Access Policies**: Rules governing how learners can access content
- **Content Assignments**: Direct assignment of content to specific learners
- **Subsidy Requests**: Learner-initiated requests for subsidized access
- **Customer Billing**: Self-service purchasing workflows via Stripe
- **Provisioning**: Enterprise customer and subscription setup workflows

The service integrates with multiple external Open edX services and follows event-driven architecture
patterns using Kafka for cross-service communication.

## System Context

Enterprise Access sits at the center of the learner credit ecosystem, coordinating between:

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Learner       │    │   Admin         │    │ Enterprise      │
│   Portal        │    │   Portal        │    │ API Customers   │
│   (Frontend)    │    │   (Frontend)    │    │(via API gateway)│
└─────────┬───────┘    └─────────┬───────┘    └─────────┬───────┘
          │                      │                      │
          └──────────────────────┼──────────────────────┘
                                 │
                        ┌────────▼────────┐
                        │                 │
                        │ Enterprise      │◄──── BFF and REST API layers
                        │ Access Service  │      (BFF: Backend-for-Frontend)
                        │                 │
                        └────────┬────────┘
                                 │
     ┌───────────────────────────┼───────────────────────────┐
     │                           │                           │
┌────▼─────┐    ┌────────▼────────┐    ┌──────▼──────┐    ┌─▼────┐
│Enterprise│    │Enterprise       │    │   LMS       │    │Stripe│
│Catalog   │    │Subsidy Service  │    │   (edxapp)  │    │      │
│          │    │                 │    │             │    │      │
└──────────┘    └─────────────────┘    └─────────────     └──────┘
```

## Core Applications Architecture

The service is organized into distinct Django applications, each handling specific business domains:

### Application Structure

```
enterprise_access/
├── apps/
│   ├── api/                    # REST API layer with versioned endpoints
│   ├── core/                   # Shared models and utilities (e.g. `core.User`)
│   ├── subsidy_access_policy/  # Core learner credit domain logic
│   ├── content_assignments/    # Assignment-based access
│   ├── subsidy_request/        # Request/approval workflows
│   ├── customer_billing/       # Stripe integration & billing for SSP subscriptions
│   ├── provisioning/           # Customer setup workflows
│   ├── workflow/               # Abstract workflow framework
│   ├── bffs/                   # Backend for Frontend layer
│   ├── api_client/             # External service clients
│   ├── events/                 # Event handling and publishing
│   └── track/                  # Analytics and tracking
```

## Domain Model Relationships

### Core Entity Relationships

See the `docs/subsidy_access_policy/README.rst` file.

```
┌─────────────────────┐
│ SubsidyAccessPolicy │◄──── Core policy entity
│                     │      - Defines access rules
│ - uuid              │      - Links to enterprise customer
│ - enterprise_uuid   │      - Contains spend limits
│ - subsidy_uuid      │      - Active/retired states
│ - spend_limit       │
│ - per_learner_limit │
│ - active            │
└──────────┬──────────┘
           │
           │ 1:1 (optional)
           │
┌──────────▼──────────┐
│ AssignmentConfig    │◄──── Assignment-based policies
│                     │      - Controls assignment lifecycle
│ - uuid              │      - Linked to access policy
│ - enterprise_uuid   │
│ - active            │
└──────────┬──────────┘
           │
           │ 1:many
           │
┌──────────▼─────────────────┐
│ LearnerContentAssignment   │◄── Individual content assignments
│                            │   - Tracks assignment state
│ - uuid                     │   - Links learner to content
│ - assignment_config        │   - Stores price and metadata
│ - learner_email            │
│ - content_key              │
│ - content_title            │
│ - content_quantity         │
│ - state (allocated,        │
│   accepted, expired)       │
└────────────────────────────┘
```

### Request/Approval Flow Models

```
┌─────────────────────┐
│ SubsidyRequest      │◄──── Base request model
│                     │      - Learner-initiated requests
│ - uuid              │      - Enterprise-scoped
│ - user              │      - State machine pattern
│ - course_id         │
│ - enterprise_uuid   │
│ - state             │
│ - reviewed_at       │
│ - reviewer          │
└─────────┬───────────┘
          │
          │ inheritance
          │
┌─────────▼───────────┐
│ LearnerCreditRequest│◄──── Specific to credit requests
│                     │      - Extends base with credit logic
│ - course_price      │      - Tracks pricing information
│ - workflow_state    │      - Integration with approval workflows
└─────────────────────┘
```

### Customer Billing Models

```
┌─────────────────────┐
│ CheckoutIntent      │◄──── Self-service purchase tracking
│                     │      - State machine for checkout process
│ - user              │      - Stripe integration
│ - enterprise_slug   │      - Reservation and fulfillment
│ - enterprise_name   │
│ - state (created,   │
│   paid, fulfilled,  │
│   errored, expired) │
│ - stripe_session_id │
│ - quantity          │
│ - price_per_seat    │
└─────────────────────┘
```

## Workflow Framework

See the `enterprise_access/apps/workflow/docs/overview.rst` file.

## External Service Integration

### API Client Architecture

The service integrates with multiple external Open edX services through dedicated client classes:

```
┌─────────────────────┐
│ External Services   │
└─────────┬───────────┘
          │
┌─────────▼───────────┐
│ API Client Layer    │
│                     │
│ ┌─────────────────┐ │  ┌──────────────────────────┐
│ │ LmsApiClient    │ │  │ • User management        │
│ └─────────────────┘ │  │ • Course enrollment      │
│                     │  │ • Enterprise membership  │
│ ┌─────────────────┐ │  └──────────────────────────┘
│ │ CatalogClient   │ │  ┌──────────────────────────┐
│ └─────────────────┘ │  │ • Content metadata       │
│                     │  │ • Catalog contains checks│
│ ┌─────────────────┐ │  │ • Pricing information    │
│ │ SubsidyClient   │ │  └──────────────────────────┘
│ └─────────────────┘ │  ┌──────────────────────────┐
│                     │  │ • Subsidy management     │
│ ┌─────────────────┐ │  │ • Transaction processing │
│ │ BrazeClient     │ │  │ • Balance checking       │
│ └─────────────────┘ │  └──────────────────────────┘
│                     │  ┌──────────────────────────┐
│ ┌─────────────────┐ │  │ • Email notifications    │
│ │ StripeClient    │ │  │ • Marketing campaigns    │
│ └─────────────────┘ │  └──────────────────────────┘
│                     │  ┌──────────────────────────┐
└─────────────────────┘  │ • Payment processing     │
                         │ • Subscription billing   │
                         │ • Webhook handling       │
                         └──────────────────────────┘
```

### Key Integration Points

1. **Enterprise Catalog Service**
   - Content metadata retrieval
   - Catalog membership validation
   - Pricing information lookup

2. **Enterprise Subsidy Service**
   - Subsidy balance management
   - Transaction creation and tracking
   - Redemption processing

3. **LMS (edx-enterprise)**
   - User authentication and authorization
   - Course enrollment processing
   - Enterprise customer membership

4. **Stripe Payment Platform**
   - Checkout session management
   - Payment processing
   - Webhook event handling

5. **Braze Marketing Platform**
   - Transactional email delivery
   - User engagement tracking
   - Campaign management

## Common Development Patterns

### Model Implementation

- All models inherit from `TimeStampedModel` for audit trails
- Use `simple_history` for model change tracking
- PII annotations required on all models (no_pii comment)
- Soft deletion patterns using `SoftDeletableModel`

### API Development

- Follow Django REST Framework patterns
- Version all public APIs (currently v1)
- Use serializers for data validation and transformation
- Implement proper filtering and pagination
- See `docs/caching.rst` for an explanation of our caching strategy

### Background Tasks

- Use Celery for asynchronous processing
- Implement idempotent task patterns
- Handle failures gracefully with retries
- Monitor task performance and errors

## Security Considerations

- OAuth2 authentication via LMS integration
- Role-based access control using Django permissions
- Secure API key management for external services
- Input validation and sanitization at API boundaries
- Audit logging for sensitive operations
