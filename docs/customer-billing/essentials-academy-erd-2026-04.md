# Essentials Academy ERD (2026-04)

This ERD extends the Teams self-service billing model to support Essentials academy-aware provisioning while retaining a single Salesforce product.

## Key Design

- Single Salesforce product remains: `01tRc000006q8UHIAY`.
- `academy_id` is the canonical academy selector.
- `academy_id` is captured at checkout, persisted in Stripe metadata, and passed into provisioning.
- Catalog provisioning uses `academy_id -> edx_catalog_id` mapping.

## Mermaid ERD

```mermaid
erDiagram
    CHECKOUT_INTENT {
      uuid id PK
      string enterprise_customer_uuid
      string enterprise_slug
      string state
      string stripe_session_id
      string stripe_customer_id
      string product_type
      string academy_id "NEW: canonical academy selector"
      int quantity
      datetime created
      datetime modified
    }

    STRIPE_EVENT_DATA {
      uuid id PK
      string stripe_event_id UK
      string event_type
      string stripe_session_id
      string product_type
      string academy_id "from metadata"
      json payload
      datetime received_at
      string processing_state
    }

    SALESFORCE_PRODUCT {
      string product2_id PK
      string name
      string product_family
      bool active
    }

    SALESFORCE_ORDER_LINE {
      string id PK
      string product2_id FK
      string academy_id "Academy_ID__c"
      decimal unit_price
      int quantity
      datetime created_date
    }

    EC_PROVISION_REQUEST {
      uuid id PK
      string enterprise_customer_uuid
      string stripe_event_id
      string stripe_session_id
      string product_type
      string academy_id
      string idempotency_key UK
      string status
      datetime requested_at
    }

    ACADEMY_CATALOG_MAP {
      string academy_id PK
      int edx_catalog_id
      string label
      bool active
      datetime updated_at
    }

    CUSTOMER_CATALOG_ENTITLEMENT {
      uuid id PK
      string enterprise_customer_uuid
      int edx_catalog_id
      string source_system
      string source_reference
      string status
      datetime provisioned_at
    }

    CHECKOUT_INTENT ||--o| STRIPE_EVENT_DATA : "produces via checkout.session.completed"
    STRIPE_EVENT_DATA ||--|| EC_PROVISION_REQUEST : "creates"
    EC_PROVISION_REQUEST }o--|| ACADEMY_CATALOG_MAP : "resolves academy_id"
    EC_PROVISION_REQUEST ||--|{ CUSTOMER_CATALOG_ENTITLEMENT : "provisions access"

    SALESFORCE_PRODUCT ||--o{ SALESFORCE_ORDER_LINE : "single Essentials product"
    STRIPE_EVENT_DATA ||--o{ SALESFORCE_ORDER_LINE : "writes academy_id"
```

## Field Contract (Final)

### academy_id

- Type: `string`
- Pattern: `^[a-z0-9-]+$`
- Allowlist examples: `ai`, `data`, `leadership`, `learning-design`
- Source of truth: checkout selection (transported via Stripe metadata)

### Stripe metadata required fields

- `product_type = essentials`
- `academy_id = <selected academy>`
- `customer_reference_id = <enterprise customer uuid>`

## Notes for Teams Diagram Alignment

- Preserve one Salesforce Product (`Product2Id = 01tRc000006q8UHIAY`).
- Do not create Salesforce product-per-academy relationships.
- Academy-specific routing must happen through `academy_id`, not `product2_id`.
