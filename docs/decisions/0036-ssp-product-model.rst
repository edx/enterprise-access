0036 SspProduct: Unified SSP Product Model
==========================================

Status
------

Accepted - May 2026

Context
-------

The SSP platform sells two product types: **Teams** (one global catalog) and **Essentials**
(per-learner Academy subscriptions). The original design used Django settings as the glue between
services — Stripe price ``lookup_key`` values, Salesforce hard-coded ``product_id`` values, and
a ``PRODUCT_ID_TO_CATALOG_QUERY_ID_MAPPING`` dict. This worked for Teams because there is exactly
one product, but extending it to multiple academies would require:

- Multiplying settings entries per academy across enterprise-access and Salesforce
- An ``EnterpriseAcademy`` model that synced metadata from enterprise-catalog (unnecessary complexity)
- Separate Teams vs. Essentials code paths throughout the BFF and provisioning layers

A secondary issue: ``invoice.paid`` Stripe webhooks only serialize ``Price`` objects, not
``Product`` objects. Any approach that reads Stripe Product metadata in webhook handlers will
silently fail.

Decision
--------

Replace ``EnterpriseAcademy`` with a single ``SspProduct`` table. Each row represents one SSP
offering (Teams or an Academy). The ``slug`` field is the universal cross-service key, stored in:

- Stripe Price metadata (``ssp_product_slug``, set via Terraform)
- Salesforce provisioning API calls (passed through from the Stripe invoice)
- This table (as primary key)

.. code-block:: python

   class SspProduct(TimeStampedModel):
       slug = models.SlugField(primary_key=True)
       stripe_price_lookup_key = models.CharField(max_length=255, unique=True)
       academy_uuid = models.UUIDField(null=True, blank=True)   # null for Teams
       catalog_query_uuid = models.UUIDField()
       license_manager_product_id_trial = models.IntegerField(null=True, blank=True)
       license_manager_product_id_paid = models.IntegerField(null=True, blank=True)
       is_active = models.BooleanField(default=True)

Academy display metadata (title, description, etc.) is **not** stored locally. The
``academy_*`` properties on ``SspProduct`` fetch and cache this data from enterprise-catalog
on demand via ``academy_api.get_cached_academy_data()``.

``CheckoutIntent`` will gain a FK to ``SspProduct`` (replacing the ``stripe_product_id`` CharField
approach), and the provisioning workflow will accept ``ssp_product_slug`` in lieu of hard-coded
``product_id`` values — enabling Salesforce to pass the slug through rather than maintaining
its own product ID config.

Consequences
------------

- Onboarding a new Academy requires one Django admin row, one Terraform price record, and no
  code changes.
- ``PRODUCT_ID_TO_CATALOG_QUERY_ID_MAPPING`` and ``SSP_PRODUCTS`` Django settings are
  eliminated once Salesforce adopts the slug passthrough (tracked separately).
- ``EnterpriseAcademy`` is deprecated and will be deleted once ``SspProduct`` is fully wired
  (no existing consumers at the time of this decision).
- Teams and Essentials share a single BFF and provisioning code path.
