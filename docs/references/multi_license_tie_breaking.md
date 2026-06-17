# Multi-License Tie-Breaking Algorithm

## Overview
When an enterprise learner has access to multiple subscription licenses that all contain the same course (because the course appears in multiple catalogs), the system must deterministically select which license should be used for enrollment tracking. This document describes the tie-breaking algorithm implemented as part of **ENT-11672** and **ENT-11683**.

---

## Business Context

### Problem Statement
Enterprise learners may have access to multiple subscription plans, each with its own catalog of courses. When catalogs overlap (the same course appears in multiple catalogs), the system needs a consistent rule to determine which license "owns" the enrollment for billing, reporting, and license capacity tracking purposes.

### Business Rule (ENT-11672)
> "The enrollment record is on the license they first activate that has the course in the catalog."

This means that when a learner activates multiple licenses over time, enrollments should be associated with the **earliest activated license** that provides access to the course.

---

## Algorithm Design

The tie-breaking algorithm uses a three-level precedence hierarchy to ensure deterministic results:

### 1. Primary: Earliest Activation Date (ASC)
The license with the **earliest `activation_date`** wins.

**Rationale:**
- Aligns with the business rule that the "first activated" license takes precedence
- Provides a clear, intuitive tie-breaker based on learner behavior
- Preserves full datetime precision (not just calendar date) to handle same-day activations

**Example:**
```python
License A: activated 2024-01-15T08:00:00Z
License B: activated 2024-02-20T10:00:00Z
Winner: License A (earlier activation)
```

### 2. Secondary: Latest Expiration Date (DESC)
When activation dates are identical, the license with the **latest `expiration_date`** wins.

**Rationale:**
- Maximizes the learner's access window to the course
- Prevents premature expiration when multiple licenses are activated simultaneously
- Business-friendly: gives learners the longest possible access

**Example:**
```python
License A: activated 2024-01-01, expires 2025-12-31
License B: activated 2024-01-01, expires 2026-12-31
Winner: License B (later expiration = longer access)
```

### 3. Tertiary: UUID Descending (DESC)
When both activation and expiration dates are identical, the license with the **lexicographically largest UUID** wins.

**Rationale:**
- Provides a stable, deterministic fallback for edge cases
- Prevents non-deterministic behavior in database query ordering
- UUID DESC ensures consistent results across all environments

**Example:**
```python
License A: uuid='zzz-license-uuid', same activation & expiration
License B: uuid='aaa-license-uuid', same activation & expiration
Winner: License A (UUID 'zzz-...' > 'aaa-...')
```

---

## Implementation Details

### Location
The tie-breaking logic is implemented in:
- **Module:** `enterprise_access/apps/bffs/handlers.py`
- **Class:** `SubscriptionLicenseProcessor`
- **Method:** `_select_best_license(licenses)`

### Date Parsing and Fallbacks
The algorithm handles edge cases robustly:

| Scenario | Behavior | Rationale |
|----------|----------|-----------|
| `activation_date` is `None` | Treated as `9999-12-31` (far future) | Licenses without activation sort last (activated licenses win) |
| `expiration_date` is `None` | Treated as `1970-01-01` (far past) | Licenses without expiration sort first in DESC order (real expirations win) |
| Unparseable date strings | Treated as sentinel values (far future/past) | Prevents crashes; valid dates always win over invalid ones |
| Full ISO datetime strings | Preserved with full precision | Same-day activations at different times are correctly ordered |

### Performance Considerations
- **Time Complexity:** O(n log n) for sorting n licenses
- **Typical Case:** Most courses map to 1-2 licenses; sorting is negligible
- **Indexing:** The `_build_catalog_index` method creates an O(1) catalog→licenses lookup to minimize redundant work
- **Caching:** Results are computed per-request and included in the BFF response

---

## Feature Flag

The multi-license behavior is controlled by a feature flag:

**Flag Name:** `ENABLE_MULTI_LICENSE_ENTITLEMENTS_BFF`

**When OFF:**
- Legacy behavior: single `subscription_license` field in API responses
- First license in the list is used (non-deterministic ordering)

**When ON:**
- New `licenses_by_catalog` field is populated in API responses
- Deterministic tie-breaking algorithm is applied
- Each course is mapped to the "best" license per the algorithm

---

## Testing

Comprehensive test coverage is provided in:
- **File:** `enterprise_access/apps/bffs/tests/test_multi_license.py`

**Key Test Scenarios:**
- Earliest activation wins (basic case)
- Expiration tie-breaking when activations match
- UUID determinism for full ties
- Same calendar date, different times preserved
- Multiple licenses tested in different input orderings (determinism verification)
- `None` and unparseable date handling
- Three-way race with multiple candidates

---

## API Impact

### Response Schema (when feature flag is ON)

**Before (Legacy):**
```json
{
  "subscription_license": {
    "uuid": "single-license-uuid",
    "status": "activated",
    "activation_date": "2024-01-01",
    ...
  }
}
```

**After (Multi-License):**
```json
{
  "subscription_license": {
    "uuid": "best-license-uuid",  // Still present for backward compatibility
    ...
  },
  "licenses_by_catalog": {
    "catalog-uuid-1": {
      "uuid": "best-license-for-catalog-1",
      ...
    },
    "catalog-uuid-2": {
      "uuid": "best-license-for-catalog-2",
      ...
    }
  }
}
```

---

## Future Considerations

### Potential Enhancements
1. **Learner-Selectable License:** Allow learners to manually choose which license to use for a course
2. **Capacity-Aware Tie-Breaking:** Prefer licenses with more remaining capacity
3. **Plan Prioritization:** Allow admins to configure which subscription plans take precedence

### Known Limitations
- Algorithm does not consider license capacity (seats remaining)
- No user-facing UI for viewing which license will be used for a course before enrollment
- Same-plan, different-catalog scenarios are treated identically (no plan-level priority)

---

## References

- **Jira Ticket:** [ENT-11672](https://2u-internal.atlassian.net/browse/ENT-11672) - Multi-license business rule definition
- **Jira Ticket:** [ENT-11683](https://2u-internal.atlassian.net/browse/ENT-11683) - BFF implementation
- **Code Location:** `enterprise_access/apps/bffs/handlers.py`
- **Test Suite:** `enterprise_access/apps/bffs/tests/test_multi_license.py`
- **Feature Flag:** `enterprise_access/toggles.py` → `ENABLE_MULTI_LICENSE_ENTITLEMENTS_BFF`

---

## Questions or Issues?

If you encounter unexpected license selection behavior, check:
1. Whether the feature flag is ON for the enterprise customer
2. The `activation_date` and `expiration_date` values for all candidate licenses
3. Whether the course appears in multiple catalogs for the learner
4. Test suite output for similar scenarios

For questions about the business logic, refer to the original Jira tickets or contact the Enterprise Access team.
