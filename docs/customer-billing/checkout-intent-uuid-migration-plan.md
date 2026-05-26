# Plan: Migrate CheckoutIntent to UUID-based Lookups

## Why?
We inadvertently placed an integer PK on the ``CheckoutIntent`` model in early phases of development. We've backpopulated a functionally equivalent candidate key with a ``uuid`` field, which follows the general edX code architecture standards. Our goal is to fully deprecate the integer ``id`` field: the practical benefit of doing so is to reduce risk of sequential id guessing by bad actors.

## General idea
Migrate CheckoutIntent from integer ID-based lookups to UUID-based lookups across enterprise-access and the checkout MFE, with backward compatibility for existing Stripe records.

## Current State
- **CheckoutIntent model** already has a `uuid` field (UUIDField, unique, default=uuid4)
- **Stripe metadata** already stores both `checkout_intent_id` and `checkout_intent_uuid` (see `stripe_api.py:50-51`)
- **API views** already support dual lookup (UUID first, then ID fallback)
- **BFF serializers** already expose both `id` and `uuid` fields

## Scope of Changes

### 1. Stripe Event Handlers (enterprise-access)
**File:** `enterprise_access/apps/customer_billing/stripe_event_handlers.py`

#### 1.1 Rename and update `get_checkout_intent_id_from_subscription()` (lines 56-73)
- Rename to `get_checkout_intent_identifier_from_subscription()`
- Return tuple `(uuid_str: str | None, id_int: int | None)` instead of just integer ID
- Prefer `checkout_intent_uuid` from metadata, fall back to `checkout_intent_id`
- Add logging for legacy records that only have ID

#### 1.2 Update `persist_stripe_event()` (lines 76-110)
- Use new identifier function
- Lookup CheckoutIntent by UUID first, fall back to ID
- Handle invalid UUID format defensively

#### 1.3 Update `get_checkout_intent_or_raise()` (lines 113-125)
- Change signature to accept `(uuid_str: str | None, id_int: int | None, event_id: str)` instead of `(checkout_intent_id, event_id)`
- Lookup by UUID first, fall back to ID
- Raise `CheckoutIntentLookupError` (custom exception) with exception chaining from the root cause

#### 1.4 Update all event handler call sites
Update these handlers to use new function signatures:
- `invoice_paid()` (line 484-486)
- `invoice_created()` (line 535-537)
- `trial_will_end()`
- `subscription_created()`
- `subscription_updated()`
- `subscription_deleted()`

**Pattern change:**
```python
# Before:
checkout_intent_id = get_checkout_intent_id_from_subscription(subscription)
checkout_intent = get_checkout_intent_or_raise(checkout_intent_id, event.id)

# After:
uuid_str, id_int = get_checkout_intent_identifier_from_subscription(subscription)
checkout_intent = get_checkout_intent_or_raise(uuid_str, id_int, event.id)
```

### 2. Tests for Stripe Event Handlers
**File:** `enterprise_access/apps/customer_billing/tests/test_stripe_event_handlers.py`

- Update test helper `_create_mock_stripe_subscription()` to support both UUID and ID in metadata
- Add test cases for:
  - UUID lookup when both UUID and ID present
  - ID fallback when UUID missing (legacy records)
  - Invalid UUID format handling
  - Both identifiers missing

### 3. Frontend (MFE) Changes
**Repository:** `frontend-app-enterprise-checkout`

**Status:** In progress

- Update polling calls to use `uuid` instead of `id` when calling `/api/v1/checkout-intent/{identifier}/`
- Store `uuid` as primary identifier in state management
- The backend already supports UUID lookup, so this is a client-side change

### 4. Salesforce Coordination (External)
- Salesforce calls `SubscriptionPlanOLIUpdateView` which already supports both `checkout_intent_id` and `checkout_intent_uuid`
- Coordinate with Salesforce team to prefer sending `checkout_intent_uuid`
- No code changes needed in enterprise-access for this

## Files to Modify

| Repository | File | Changes |
|------------|------|---------|
| enterprise-access | `enterprise_access/apps/customer_billing/stripe_event_handlers.py` | Core lookup functions and all event handlers |
| enterprise-access | `enterprise_access/apps/customer_billing/tests/test_stripe_event_handlers.py` | Test helper and new test cases |
| frontend-app-enterprise-checkout | TBD after exploration | Polling calls to use UUID |

## Out of Scope (Deferred)
- **Celery tasks** (`tasks.py`): These are internal-only and receive CheckoutIntent objects from event handlers. Event handlers can continue passing `checkout_intent.id` to tasks. Migrating tasks to UUID adds complexity with minimal benefit.
- **Removing ID-based lookups**: This will be a future breaking change after transition period

## Implementation Order
1. Update `stripe_event_handlers.py` core functions
2. Update all event handler call sites
3. Update/add tests for backend changes
4. Explore MFE repo and identify files to change
5. Implement MFE changes (polling to use UUID)
6. Salesforce coordination (external team - no code changes needed)

## Backward Compatibility
- All changes prefer UUID but fall back to ID
- Existing Stripe records without UUID in metadata will continue to work
- Logging will identify legacy records for monitoring migration progress

## Risk Mitigation
- No database migrations required
- Pure application logic changes
- Fallback to ID ensures existing records work
- Easy rollback if issues arise
