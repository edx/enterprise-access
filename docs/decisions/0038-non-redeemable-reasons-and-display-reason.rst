0038 Non-redeemable reasons by budget type, and the one a learner sees
**********************************************************************

Status
======
**Proposed** (October 2026)

Context
=======
A learner credit budget is a ``SubsidyAccessPolicy`` record. Every eligibility check on a budget
either passes or returns a short reason slug, and those slugs drive the message a learner or admin
sees. The slugs are defined in ``subsidy_access_policy/constants.py``, raised in
``subsidy_access_policy/models.py`` and turned into learner-facing text in
``api/v1/views/subsidy_access_policy.py`` (``_get_user_message_for_reason``). Nothing documented
which budget type raises which reason, in what order, or which reason wins when a customer has
several budgets.

That gap surfaced in `ENT-12416`_. A learner over their per-learner spend limit was told
*"You can't enroll right now because your funds expired."* The customer had a live,
spend-capped budget alongside older budgets that were still active and unretired but funded by
expired subsidies. ``can-redeem`` evaluates every active, unretired budget, sorts expired subsidies
first (soonest expiration), and used to display the first reason it found, so the expired budget's
reason masked the one that actually blocked the learner.

.. _ENT-12416: https://2u-internal.atlassian.net/browse/ENT-12416

Budget types
------------
``PolicyTypes`` (``subsidy_access_policy/constants.py``) lists every concrete budget type. All
three are proxy models of ``SubsidyAccessPolicy``.

.. list-table::
   :header-rows: 1
   :widths: 24 8 30 38

   * - Policy type
     - Priority
     - Limits it enforces
     - Type-specific reasons
   * - ``AssignedLearnerCreditAccessPolicy``
     - 0
     - ``spend_limit``; content must be assigned by an admin
     - ``reason_learner_not_assigned_content``, ``reason_learner_assignment_cancelled``,
       ``reason_learner_assignment_failed``, ``reason_learner_assignment_expired``,
       ``reason_learner_assignment_reversed``
   * - ``PerLearnerSpendCreditAccessPolicy``
     - 1
     - ``spend_limit``; optional ``per_learner_spend_limit`` (USD cents, net of committed
       reversals)
     - ``learner_max_spend_reached``. When Browse & Request (BnR) is enabled, ``can_redeem``
       instead runs as an assigned budget and raises the assignment reasons above.
   * - ``PerLearnerEnrollmentCreditAccessPolicy``
     - 1
     - ``spend_limit``; optional ``per_learner_enrollment_limit`` (count of transactions)
     - ``learner_max_enrollments_reached``

Priority comes from ``AssignedCreditPolicyMixin`` and ``CreditPolicyMixin``.
``SUBSCRIPTION_POLICY_TYPE_PRIORITY`` (2) is defined but no policy type uses it.

Decision
========
We record the full reason catalogue and evaluation order here, as the reference for anyone adding
a budget type or a reason, and we choose ``display_reason`` by ranking reasons instead of taking
the first one.

1. How one budget decides
-------------------------
``SubsidyAccessPolicy.can_redeem`` runs the shared checks in a fixed order, and each subclass adds
its own check only after they all pass. The first failing check is the budget's reason. Checks are
ordered from the learner's state, to the content, to the budget's money, as the method's docstring
describes.

.. mermaid::

   flowchart TD
       START(["policy.can_redeem(learner, content)"]) --> A{"Policy active<br/>and not retired?"}
       A -- no --> R_PE["policy_expired"]
       A -- yes --> B{"Learner linked to<br/>the customer?<br/><i>skipped by can-redeem unless<br/>staff override lms_user_id</i>"}
       B -- no --> R_NE["learner_not_in_enterprise"]
       B -- yes --> C{"Policy has groups and<br/>learner is in none?"}
       C -- yes --> R_NG["learner_not_in_enterprise_group"]
       C -- no --> D{"Content in the policy's catalog<br/>and its metadata found?"}
       D -- no --> R_NC["content_not_in_catalog"]
       D -- yes --> E{"Past the enroll-by date and<br/>late redemption not allowed?"}
       E -- yes --> R_DL["beyond_enrollment_deadline"]
       E -- no --> F{"Subsidy active now?<br/><i>enterprise-subsidy can_redeem</i>"}
       F -- no --> R_SE["subsidy_expired"]
       F -- yes --> G{"Subsidy balance<br/>covers the price?"}
       G -- no --> R_NV["not_enough_value_in_subsidy"]
       G -- yes --> H{"Policy spend_limit<br/>covers the price?"}
       H -- no --> R_SL["policy_spend_limit_reached"]
       H -- yes ---> T{"Policy type"}

       T -- "PerLearnerEnrollment" --> I{"Learner transactions >=<br/>per_learner_enrollment_limit?"}
       I -- yes --> R_ME["learner_max_enrollments_reached"]
       T -- "PerLearnerSpend<br/>(BnR off)" --> J{"Net learner spend + price ><br/>per_learner_spend_limit?"}
       J -- yes --> R_MS["learner_max_spend_reached"]
       T -- "Assigned, or<br/>PerLearnerSpend with BnR on" --> K{"Learner's assignment<br/>for this content"}
       K -- "none or accepted" --> R_NA["reason_learner_not_assigned_content"]
       K -- cancelled --> R_AC["reason_learner_assignment_cancelled"]
       K -- errored --> R_AF["reason_learner_assignment_failed"]
       K -- expired --> R_AE["reason_learner_assignment_expired"]
       K -- reversed --> R_AR["reason_learner_assignment_reversed"]

       I -- no --> OK(["Redeemable"])
       J -- no --> OK
       K -- allocated --> OK

       classDef expiry fill:#fde2e1,stroke:#c0392b
       classDef notApplicable fill:#fff3cd,stroke:#b7950b
       classDef blocking fill:#d6eaf8,stroke:#2471a3
       class R_PE,R_SE expiry
       class R_NG,R_NC,R_NA notApplicable
       class R_NE,R_DL,R_NV,R_SL,R_ME,R_MS,R_AC,R_AF,R_AE,R_AR blocking

Colours match the ``display_reason`` ranks in section 3: red is expiry, yellow is a budget that
doesn't apply, blue is a budget that applies but blocks the learner.

2. Reason catalogue
-------------------
*Raised by* lists the budget types that can produce the reason. *Where* lists the operations that
raise it: ``can-redeem`` and ``redeem`` (learner enrolling, ``can_redeem``), ``allocate`` (admin
assigning, ``AssignedLearnerCreditAccessPolicy.can_allocate``), ``approve`` (admin approving BnR
requests, ``can_approve`` and ``can_allocate_all``) and ``can-request`` (learner opening a BnR
request).

.. list-table::
   :header-rows: 1
   :widths: 22 12 22 18 16 10

   * - Reason slug
     - Category
     - Raised when
     - Raised by
     - Where
     - Display rank
   * - ``policy_expired``
     - Expiry
     - The policy is inactive or retired (``is_redemption_enabled`` is false).
     - All types
     - redeem, allocate, approve. ``can-redeem`` only evaluates active, unretired policies, so it
       never shows this one.
     - 2
   * - ``subsidy_expired``
     - Expiry
     - The subsidy is outside its active window, either expired or not started yet.
     - All types
     - can-redeem, redeem, allocate, approve, can-request
     - 2
   * - ``learner_not_in_enterprise``
     - Learner membership
     - The learner has no enterprise user record for the customer.
     - All types
     - redeem; can-redeem only with a staff ``lms_user_id`` override
     - 0
   * - ``learner_not_in_enterprise_group``
     - Not applicable
     - The policy is restricted to groups and the learner is in none of them.
     - All types
     - redeem; can-redeem only with a staff ``lms_user_id`` override
     - 1
   * - ``content_not_in_catalog``
     - Not applicable
     - The policy's catalog doesn't contain the content, or the subsidy has no metadata for it.
     - All types
     - can-redeem, redeem, allocate, approve (per request), can-request
     - 1
   * - ``beyond_enrollment_deadline``
     - Deadline
     - Now is past the content's enroll-by date and late redemption isn't allowed.
     - All types
     - can-redeem, redeem
     - 0
   * - ``not_enough_value_in_subsidy``
     - Out of budget
     - The subsidy's remaining balance can't cover the price (for allocation and approval: the
       already-allocated amount plus the new price).
     - All types
     - can-redeem, redeem, allocate, approve
     - 0
   * - ``policy_spend_limit_reached``
     - Out of budget
     - The policy's ``spend_limit`` can't cover the price (for allocation and approval: spent plus
       allocated plus the new price).
     - All types
     - can-redeem, redeem, allocate, approve
     - 0
   * - ``learner_max_spend_reached``
     - Limit reached
     - The learner's spend, net of committed reversals, plus the price exceeds
       ``per_learner_spend_limit``.
     - ``PerLearnerSpendCreditAccessPolicy`` (BnR off)
     - can-redeem, redeem
     - 0
   * - ``learner_max_enrollments_reached``
     - Limit reached
     - The learner's transaction count is at or above ``per_learner_enrollment_limit``.
     - ``PerLearnerEnrollmentCreditAccessPolicy``
     - can-redeem, redeem
     - 0
   * - ``reason_learner_not_assigned_content``
     - Not assigned
     - The learner has no allocated assignment for the content. An ``accepted`` assignment also
       maps here; redemption checks already return earlier in that case.
     - ``AssignedLearnerCreditAccessPolicy``; ``PerLearnerSpendCreditAccessPolicy`` with BnR on
     - can-redeem, redeem
     - 1
   * - ``reason_learner_assignment_cancelled``
     - Not assigned
     - The learner's assignment was cancelled.
     - Same as above
     - can-redeem, redeem
     - 0
   * - ``reason_learner_assignment_failed``
     - Not assigned
     - The learner's assignment errored.
     - Same as above
     - can-redeem, redeem
     - 0
   * - ``reason_learner_assignment_expired``
     - Not assigned
     - The learner's assignment expired.
     - Same as above
     - can-redeem, redeem
     - 0
   * - ``reason_learner_assignment_reversed``
     - Not assigned
     - The learner's assignment was reversed.
     - Same as above
     - can-redeem, redeem
     - 0
   * - ``REASON_BNR_NOT_ENABLED``
     - Not applicable
     - An admin approves a request on a policy without an active BnR config. The constant holds
       the message ("This policy does not support BNR."), not a slug.
     - All types
     - approve
     - n/a

Admin and BnR flows raise a subset of these, in their own order:

.. mermaid::

   flowchart TB
       subgraph AL["allocate — AssignedLearnerCreditAccessPolicy.can_allocate"]
           direction LR
           A1["policy_expired"] --> A2["content_not_in_catalog"] --> A3["subsidy_expired"]
           A3 --> A4["not_enough_value_in_subsidy"] --> A5["policy_spend_limit_reached"]
       end
       subgraph AP["approve — can_approve, then can_allocate_all"]
           direction LR
           P0["REASON_BNR_NOT_ENABLED"] --> P1["policy_expired"] --> P2["subsidy_expired"]
           P2 --> P3["content_not_in_catalog<br/>or PriceValidationError<br/><i>per request, partial success</i>"]
           P3 --> P4["not_enough_value_in_subsidy"] --> P5["policy_spend_limit_reached"]
       end
       subgraph CR["can-request — SubsidyAccessPolicyRedeemViewset.can_request"]
           direction LR
           C1["no active policies"] --> C2["no BnR-enabled policies"] --> C3["subsidy_expired"]
           C3 --> C4["content_not_in_catalog"] --> C5["existing open request"]
       end
       AL ~~~ AP ~~~ CR

The ``can-request`` checks run across all of the customer's BnR budgets at once rather than per
budget, and three of them return plain text rather than a slug.

3. Which reason the learner sees
--------------------------------
``can-redeem`` evaluates every budget for the content and groups budgets by reason. When none is
redeemable it returns all the reasons, plus a single ``display_reason`` that the learner portal
shows as-is.

.. mermaid::

   flowchart TD
       Q(["GET can-redeem for a content key"]) --> RD{"Learner already redeemed<br/>this content?"}
       RD -- yes --> DONE(["has_successful_redemption, no reasons"])
       RD -- no --> P["Active, unretired policies for the customer"]
       P --> S["Sort: type priority (Assigned 0, credit 1),<br/>then soonest subsidy expiration, then lowest balance"]
       S --> EV["can_redeem on each policy"]
       EV --> X{"Any redeemable?"}
       X -- yes --> OK(["redeemable_subsidy_access_policy = first redeemable"])
       X -- no --> BK["Group policies by reason,<br/>in first-seen order: reasons[]"]
       BK --> RK["display_reason = first reason with the lowest rank"]
       RK --> R0["Rank 0: applies but blocks<br/>limits, out of budget, deadline,<br/>assignment state, not in enterprise"]
       RK --> R1["Rank 1: doesn't apply<br/>not in catalog, not assigned, not in group"]
       RK --> R2["Rank 2: expired<br/>subsidy_expired, policy_expired"]

       classDef expiry fill:#fde2e1,stroke:#c0392b
       classDef notApplicable fill:#fff3cd,stroke:#b7950b
       classDef blocking fill:#d6eaf8,stroke:#2471a3
       class R2 expiry
       class R1 notApplicable
       class R0 blocking

The ranking lives in ``_display_reason_rank`` (``api/v1/views/subsidy_access_policy.py``):

* **Rank 0, applies but blocks.** The budget would fund this learner and content but something is
  in the way: a per-learner limit, the budget or subsidy is out of money, the enroll-by date has
  passed, or the learner's assignment is in a terminal state. This is what the learner can act on.
* **Rank 1, doesn't apply.** The budget was never going to fund this learner and content. Assigned
  budgets sort first, so without this rank an assigned budget with nothing assigned would mask a
  browse-and-enroll budget's limit.
* **Rank 2, expired.** The budget is no longer usable at all. It's shown only when every budget
  is expired.

``min()`` returns the first reason with the lowest rank, so the sort order above still decides
between reasons of the same rank. The ``reasons`` list and the response shape are unchanged, and
``redeem`` and ``allocate``, which only ever pass a single reason, are unaffected.

4. Learner-facing messages
--------------------------
``_get_user_message_for_reason`` maps slugs to ``MissingSubsidyAccessReasonUserMessages``. The
"no admins" variants are used when the customer has no admin contact.

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Reason slug
     - Message
   * - ``subsidy_expired``
     - ``ORGANIZATION_EXPIRED_FUNDS``: "...because your funds expired."
   * - ``policy_expired``, ``not_enough_value_in_subsidy``, ``policy_spend_limit_reached``
     - ``ORGANIZATION_NO_FUNDS``: "...because your organization doesn't have enough funds."
   * - ``learner_max_spend_reached``, ``learner_max_enrollments_reached``
     - ``LEARNER_LIMITS_REACHED``: "...because of limits set by your organization."
   * - ``content_not_in_catalog``
     - ``CONTENT_NOT_IN_CATALOG``
   * - ``beyond_enrollment_deadline``
     - ``BEYOND_ENROLLMENT_DEADLINE``
   * - ``learner_not_in_enterprise``
     - ``LEARNER_NOT_IN_ENTERPRISE``
   * - ``reason_learner_not_assigned_content``, ``reason_learner_assignment_failed``
     - ``LEARNER_NOT_ASSIGNED_CONTENT``
   * - ``reason_learner_assignment_cancelled``
     - ``LEARNER_ASSIGNMENT_CANCELED``
   * - ``learner_not_in_enterprise_group``, ``reason_learner_assignment_expired``,
       ``reason_learner_assignment_reversed``
     - **None.** The learner portal has no fallback for these slugs either.

Consequences
============
* A learner blocked by a live budget now sees that budget's reason, such as
  ``learner_max_spend_reached``, instead of an expired or inapplicable budget's reason.
* The ranking is a product decision, not just a technical one. A new reason defaults to rank 0,
  so whoever adds one must decide whether it means "applies but blocks", "doesn't apply" or
  "expired" and add it to ``NOT_APPLICABLE_POLICY_REASONS`` or ``EXPIRED_POLICY_REASONS`` if needed.
* **Known gaps, not addressed by this decision:**

  * Three slugs have no learner-facing message (section 4). With the ranking, an expired or
    reversed assignment can now be chosen over an expired budget's message, so these are worth
    filling in.
  * ``policy_expired`` reads "doesn't have enough funds", not "expired".
  * Active, unretired budgets funded by expired subsidies are still evaluated on every
    ``can-redeem`` call. Retiring them is an operational clean-up that also fixes the message
    for a customer immediately.
  * ``per_learner_spend_limit`` isn't checked on the BnR path: with BnR on, ``can_redeem`` runs as
    an assigned budget and ``can_allocate_all`` checks only the subsidy balance and
    ``spend_limit``.
  * For a learner's own request, ``can-redeem`` passes ``skip_customer_user_check=True``
    (``evaluate_policies``), so ``learner_not_in_enterprise`` and
    ``learner_not_in_enterprise_group`` only come from ``redeem`` or a staff ``lms_user_id``
    override. ``can-redeem`` doesn't reflect a budget's group restriction for the learner.
  * ``learner_max_enrollments_reached`` counts every transaction for the learner, including
    reversed ones, so a refunded enrollment still uses up the enrollment limit.

Alternatives Considered
=======================
* **Skip only expiration reasons.** Smaller, and it fixes a customer whose only other budget is
  browse-and-enroll. It doesn't fix a customer with a live assigned budget: assigned budgets sort
  first, so "this course is not assigned to you" would still mask the per-learner limit.
* **Stop evaluating budgets on expired subsidies.** Removes the masking at the source, but then a
  customer whose budgets have *all* expired would get no reason at all, instead of the correct
  "your funds expired".
* **Re-sort policies by liveness before evaluating.** Changes which budget redeems when several
  are redeemable, which is a larger behavioural change than choosing a message.

References
==========
* ADR 0004 — add access policy functionality
* ADR 0021 — transaction aggregates (per-learner spend, net of committed reversals)
* `ENT-12416`_ — the learner support request that prompted this
* ``subsidy_access_policy/constants.py`` — ``PolicyTypes``, the ``REASON_*`` slugs and
  ``MissingSubsidyAccessReasonUserMessages``
* ``subsidy_access_policy/models.py`` — ``SubsidyAccessPolicy.can_redeem`` and the per-type
  overrides, ``can_allocate``, ``can_allocate_all``, ``can_approve``
* ``api/v1/views/subsidy_access_policy.py`` — ``evaluate_policies``,
  ``_get_reasons_for_no_redeemable_policies``, ``_display_reason_rank``, ``can_request``
