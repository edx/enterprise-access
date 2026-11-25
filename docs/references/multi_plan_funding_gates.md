# Multi-plan learner credit funding: on/off controls

Introduced for **ENT-12428** (epic ENT-12102). Multi-plan funding lets one enrollment be paid from more than one
learner credit budget. This note covers only the controls that turn it on; the funding logic itself ships separately.

## The three controls

| Control | Where | Scope | Who sets it |
|---|---|---|---|
| `enterprise_access.multi_plan_funding` | enterprise-access Django admin → Waffle → **Switches** | Global kill switch | Engineering / release |
| `SubsidyRequestCustomerConfiguration.enable_multi_plan_funding` | enterprise-access Django admin → Subsidy request customer configurations | One customer | Enterprise Customer Success |
| `enterprise.multi_plan_funding_enabled` | LMS Django admin → Waffle → **Flags** (edx-enterprise) | Admin portal UI, per user | Engineering / release |

The first two are backend gates. Check both with `subsidy_request.models.is_multi_plan_enabled(enterprise_customer_uuid)`,
which is `True` only when the switch is on **and** the customer's configuration exists with the field on. A customer
with no configuration row is off. The switch is read first, so nothing is queried while it's off.

The third is not read by enterprise-access. The LMS returns it to the admin portal as
`enterprise_features.enterprise_multi_plan_funding_enabled`, and it only shows or hides the multi-plan UI.

## The customer setting in the API

`enable_multi_plan_funding` is returned by `GET /api/v1/customer-configurations/` (list and detail), because the
admin portal shows the Settings → Multi-plan funding tab only for customers who have it. It is **read-only**: a
`POST` or `PATCH` that includes it succeeds but leaves the stored value unchanged. Only Django admin changes it, and
every change is kept in the model's history with the user who made it.

The API returns the stored value, not the result of `is_multi_plan_enabled`, so a customer can show as on while the
global switch is off.

## Turning it on for a customer

1. Turn on `enterprise_access.multi_plan_funding` (once per environment).
2. Tick **Enable multi plan funding** on the customer's subsidy request customer configuration.
3. For the admin portal UI, also turn on the LMS flag `enterprise.multi_plan_funding_enabled` for the admins who
   should see it.

Turning the switch off stops multi-plan funding for every customer without touching their settings.
