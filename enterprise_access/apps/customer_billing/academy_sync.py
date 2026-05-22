"""Sync helpers for importing academy metadata from Enterprise Catalog into EnterpriseAcademy."""

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.apps import apps

from enterprise_access.apps.api_client.enterprise_catalog_client import EnterpriseCatalogApiClient

logger = logging.getLogger(__name__)


def _get_enterprise_academy_model():
    """Resolve EnterpriseAcademy lazily so this module can import without model migrations."""
    return apps.get_model('customer_billing', 'EnterpriseAcademy')


@dataclass
class AcademySyncResult:
    """Container for sync summary counters."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    deactivated: int = 0
    errors: int = 0


SYNC_UPDATE_FIELDS = (
    'long_name',
    'description',
    'marketing_url',
    'thumbnail_url',
    'tags',
    'stripe_product_id',
    'stripe_price_lookup_key',
    'catalog_query_uuid',
    'product_key',
    'slug',
    'is_active',
    'display_order',
)


def _normalize_catalog_query_uuid(value: Any) -> str | None:
    """Normalize catalog query UUID to a canonical string, if valid."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str):
        return None

    raw_uuid = value.strip()
    if not raw_uuid:
        return None

    try:
        return str(UUID(raw_uuid))
    except (TypeError, ValueError):
        return None


def _normalize_catalog_academy(
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """Map one Enterprise Catalog academy payload to EnterpriseAcademy model fields."""
    if not isinstance(item, dict):
        return None

    name = (item.get('name') or '').strip()
    if not name:
        return None

    product_key = (item.get('product_key') or '').strip()
    slug = (item.get('slug') or '').strip()
    stripe_price_lookup_key = (item.get('stripe_price_lookup_key') or '').strip()
    if not product_key or not slug or not stripe_price_lookup_key:
        return None

    long_name = item.get('long_name')
    description = item.get('description')
    marketing_url = item.get('marketing_url')
    thumbnail_url = item.get('thumbnail_url')
    stripe_product_id = item.get('stripe_product_id')

    display_order = item.get('display_order', 0)
    display_order = int(display_order or 0)

    tags = item.get('tags')
    normalized_tags = tags if isinstance(tags, list) else []

    return {
        'name': name,
        'long_name': long_name.strip() if isinstance(long_name, str) else '',
        'description': description.strip() if isinstance(description, str) else '',
        'marketing_url': marketing_url.strip() if isinstance(marketing_url, str) else '',
        'thumbnail_url': thumbnail_url.strip() if isinstance(thumbnail_url, str) else '',
        'tags': normalized_tags,
        'stripe_product_id': stripe_product_id.strip() if isinstance(stripe_product_id, str) else '',
        'stripe_price_lookup_key': stripe_price_lookup_key,
        'catalog_query_uuid': _normalize_catalog_query_uuid(item.get('catalog_query_uuid')),
        'product_key': product_key,
        'slug': slug,
        'is_active': bool(item.get('is_active', True)),
        'display_order': display_order,
    }


def sync_enterprise_academies_from_enterprise_catalog(
    academy_uuid=None,
    deactivate_missing: bool = False,
    dry_run: bool = False,
) -> AcademySyncResult:
    """Sync Enterprise Catalog academy entries into EnterpriseAcademy rows."""
    result = AcademySyncResult()
    enterprise_academy_model = _get_enterprise_academy_model()
    client = EnterpriseCatalogApiClient()
    get_academies = getattr(client, 'get_academies', None)
    if not callable(get_academies):
        raise AttributeError('EnterpriseCatalogApiClient.get_academies is required for academy sync')
    payload = get_academies(academy_uuid=academy_uuid)  # pylint: disable=not-callable
    academy_items = payload if isinstance(payload, list) else []

    objects_to_upsert = []
    seen_names: set[str] = set()

    for item in academy_items:
        try:
            normalized = _normalize_catalog_academy(item)
        except Exception as exc:  # pylint: disable=broad-except
            result.errors += 1
            logger.exception('Failed normalizing academy payload: %s', exc)
            continue

        if normalized is None:
            result.skipped += 1
            continue

        name = normalized['name']
        seen_names.add(name)
        current = enterprise_academy_model.objects.filter(name__iexact=name).first()
        if current is not None:
            seen_names.add(current.name)

        if normalized['catalog_query_uuid'] is None and current and current.catalog_query_uuid is not None:
            normalized['catalog_query_uuid'] = current.catalog_query_uuid

        if current is None:
            result.created += 1
        else:
            has_changes = any(getattr(current, field_name) != normalized[field_name] for field_name in normalized)
            if not has_changes:
                result.unchanged += 1
                continue
            result.updated += 1

        objects_to_upsert.append(enterprise_academy_model(**normalized))

    if objects_to_upsert and not dry_run:
        try:
            enterprise_academy_model.objects.bulk_create(
                objects_to_upsert,
                update_conflicts=True,
                unique_fields=['name'],
                update_fields=list(SYNC_UPDATE_FIELDS),
            )
        except Exception as exc:  # pylint: disable=broad-except
            result.errors += len(objects_to_upsert)
            logger.exception('Bulk academy upsert failed: %s', exc)

    if deactivate_missing and seen_names:
        stale_queryset = enterprise_academy_model.objects.filter(is_active=True).exclude(name__in=seen_names)
        stale_count = stale_queryset.count()
        if stale_count and not dry_run:
            stale_queryset.update(is_active=False)
        result.deactivated += stale_count

    return result
