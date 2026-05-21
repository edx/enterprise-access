"""Sync helpers for importing academy metadata from Enterprise Catalog into EnterpriseAcademy."""

import logging
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.apps import apps
from django.db import transaction
from django.utils.text import slugify

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


def _first_non_empty(*values: Any) -> str:
    """Return first non-empty string representation from values."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
            continue
        if isinstance(value, dict):
            rendered = value.get('rendered')
            if isinstance(rendered, str) and rendered.strip():
                return rendered.strip()
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return ''


def _to_slug(raw_value: str, fallback_prefix: str, item_id: Any) -> str:
    """Convert to slug, with deterministic fallback if slugify returns empty."""
    slug_value = slugify(raw_value or '')
    if slug_value:
        return slug_value

    fallback_slug = slugify(fallback_prefix or '')
    item_slug = slugify(str(item_id or ''))

    if fallback_slug and item_slug:
        return f'{fallback_slug}-{item_slug}'
    if item_slug:
        return item_slug
    if fallback_slug:
        return fallback_slug
    return 'item'


def _to_lookup_token(raw_value: str) -> str:
    """Normalize a value into a Stripe lookup-key token format."""
    value = (raw_value or '').strip().lower()
    if not value:
        return ''
    value = value.replace('&', ' and ')
    value = re.sub(r'[^a-z0-9]+', '_', value)
    value = re.sub(r'_+', '_', value).strip('_')
    return value


def _default_stripe_lookup_key(name: str, product_key: str) -> str:
    """Build deterministic Stripe lookup key when source does not provide one."""
    name_token = _to_lookup_token(name)
    if name_token:
        return f'essentials_{name_token}_academy_yearly'

    product_token = _to_lookup_token(product_key)
    if product_token:
        return f'essentials_{product_token}_academy_yearly'

    return ''


def _extract_payload_list(payload: Any) -> list[dict[str, Any]]:
    """Extract list payload from paginated or plain-list wrapper formats."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []

    for key in ('results', 'items', 'academies', 'catalogs'):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _extract_catalog_query_uuid(*sources: Any) -> str | None:
    """Extract a catalog query UUID from heterogeneous payload fields."""
    for source in sources:
        if source is None:
            continue

        if isinstance(source, bool):
            continue

        if isinstance(source, UUID):
            return str(source)

        if isinstance(source, str):
            value = source.strip()
            try:
                return str(UUID(value))
            except (TypeError, ValueError):
                continue

        # Ignore legacy integer ids when extracting UUID values.
        if isinstance(source, int):
            continue

        if isinstance(source, dict):
            nested = _extract_catalog_query_uuid(
                source.get('catalog_query_uuid'),
                source.get('catalog_query_id'),
                source.get('id'),
                source.get('pk'),
                source.get('uuid'),
            )
            if nested is not None:
                return nested
            continue

        if isinstance(source, list):
            for item in source:
                nested = _extract_catalog_query_uuid(item)
                if nested is not None:
                    return nested

    return None


def _normalize_catalog_academy(
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """Map one Enterprise Catalog academy payload to EnterpriseAcademy model fields."""
    name = _first_non_empty(item.get('name'), item.get('title'), item.get('short_name'))
    if not name:
        return None

    product_key_raw = _first_non_empty(item.get('product_key'), item.get('slug'), name)
    slug_raw = _first_non_empty(item.get('slug'), product_key_raw, name)

    product_key = _to_slug(product_key_raw, 'academy-product', name)
    slug = _to_slug(slug_raw, 'academy', name)

    metadata_raw = item.get('metadata')
    metadata: dict[str, Any] = metadata_raw if isinstance(metadata_raw, dict) else {}

    stripe_lookup_key = _first_non_empty(
        item.get('stripe_price_lookup_key'),
        metadata.get('stripe_price_lookup_key'),
        _default_stripe_lookup_key(name, product_key),
    )

    return {
        'name': name,
        'long_name': _first_non_empty(item.get('long_name'), item.get('title'), name),
        'description': _first_non_empty(item.get('description'), item.get('summary')),
        'marketing_url': _first_non_empty(item.get('marketing_url'), item.get('url')),
        'thumbnail_url': _first_non_empty(item.get('thumbnail_url'), item.get('image_url')),
        'tags': item.get('tags') if isinstance(item.get('tags'), list) else [],
        'stripe_product_id': _first_non_empty(item.get('stripe_product_id'), metadata.get('stripe_product_id')),
        'stripe_price_lookup_key': stripe_lookup_key,
        'catalog_query_uuid': _extract_catalog_query_uuid(
            item.get('catalog_query_uuid'),
            item.get('catalog_query_id'),
            metadata.get('catalog_query_uuid'),
            metadata.get('catalog_query_id'),
            item.get('catalog_queries'),
            metadata.get('catalog_queries'),
        ),
        'product_key': product_key,
        'slug': slug,
        'is_active': bool(item.get('is_active', True)),
        'display_order': int(item.get('display_order', 0) or 0),
    }


def fetch_enterprise_catalog_academies(academy_uuid=None) -> list[dict[str, Any]]:
    """Fetch academy payload from Enterprise Catalog."""
    client = EnterpriseCatalogApiClient()
    get_academies = getattr(client, 'get_academies', None)
    if not callable(get_academies):
        raise AttributeError('EnterpriseCatalogApiClient.get_academies is required for academy sync')
    payload = get_academies(academy_uuid=academy_uuid)  # pylint: disable=not-callable
    return _extract_payload_list(payload)


@transaction.atomic
def sync_enterprise_academies_from_enterprise_catalog(
    academy_uuid=None,
    deactivate_missing: bool = False,
    dry_run: bool = False,
) -> AcademySyncResult:
    """Sync Enterprise Catalog academy entries into EnterpriseAcademy rows."""
    result = AcademySyncResult()
    enterprise_academy_model = _get_enterprise_academy_model()

    academy_items = fetch_enterprise_catalog_academies(academy_uuid=academy_uuid)
    seen_names: set[str] = set()

    for item in academy_items:
        normalized = _normalize_catalog_academy(item)
        if not normalized:
            result.skipped += 1
            continue

        name = normalized['name']
        seen_names.add(name)

        current = enterprise_academy_model.objects.filter(name__iexact=name).first()
        # Preserve existing UUID when payload does not include one.
        if normalized.get('catalog_query_uuid') is None and current and current.catalog_query_uuid is not None:
            normalized['catalog_query_uuid'] = current.catalog_query_uuid

        try:
            if current is None:
                if not dry_run:
                    enterprise_academy_model.objects.create(**normalized)
                result.created += 1
                continue

            has_changes = any(
                getattr(current, field_name) != field_value
                for field_name, field_value in normalized.items()
            )
            if not has_changes:
                result.unchanged += 1
                continue

            if not dry_run:
                for field_name, field_value in normalized.items():
                    setattr(current, field_name, field_value)
                current.save()
            result.updated += 1
        except Exception as exc:  # pylint: disable=broad-except
            result.errors += 1
            logger.exception('Failed syncing academy payload (name=%s): %s', name, exc)

    if deactivate_missing and seen_names:
        stale_queryset = enterprise_academy_model.objects.filter(
            is_active=True,
        ).exclude(name__in=seen_names)
        stale_count = stale_queryset.count()
        if stale_count and not dry_run:
            stale_queryset.update(is_active=False)
        result.deactivated += stale_count

    return result
