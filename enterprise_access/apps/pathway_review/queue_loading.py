"""
Reading a queue file into review items, for the management command and the admin alike.

The queue is produced offline: pathways are assembled, judged and chosen elsewhere, and the
bench only shows them to people. That split is the reason this app owns no pipeline logic, and
it is why loading is a file import rather than a form -- nobody types a pathway in here.

One loader, two front doors. The command is for a shell; the admin upload is for whoever runs a
review round and has no shell. Both call :func:`load_queue`, so what a file means cannot drift
between them.

Three properties are worth stating, because they are what make an upload safe to hand to
someone who cannot inspect the database afterwards:

* **All or nothing.** A file with one bad record loads none of it. A half-loaded queue would be
  discovered by a reviewer, mid-round, as a pathway that renders wrong.
* **It can be rehearsed.** A dry run does the whole thing, reports exactly what would change,
  and rolls back.
* **Votes are never silently orphaned.** Deactivating items that already carry votes is
  reported before it happens, because that is how an in-flight round would be cut short.
"""

import json

from django.db import transaction

from enterprise_access.apps.pathway_review.models import PathwayReviewItem, ReviewPool

LEVELS = ('Introductory', 'Intermediate', 'Advanced')

#: Fields a queue record must carry. The rest have defaults, and a record that is missing one of
#: these cannot be rendered at all, so it is rejected rather than stored half-formed.
REQUIRED_RECORD_FIELDS = ('id', 'family_key', 'pathway', 'careers_covered', 'mix', 'pool', 'courses')
#: Fields each course in a record must carry, for the same reason.
REQUIRED_COURSE_FIELDS = ('step', 'level', 'key', 'title')
#: The id column's width, read once here rather than at every record.
ITEM_ID_MAX_LENGTH = PathwayReviewItem._meta.get_field('item_id').max_length

#: How many problems to name before saying "and N more". A reviewer fixing a generator does not
#: need every line, and a page listing thousands helps nobody.
PROBLEMS_SHOWN = 20


class QueueError(ValueError):
    """A queue file that cannot be loaded, with every reason it cannot."""

    def __init__(self, message, problems=()):
        self.problems = list(problems)
        super().__init__(message)


def build_payload(record, course_descriptions):
    """
    Assemble the reviewer-visible half of one queue record.

    Only what a browser may see: ``pool`` and ``control_key`` are set on the item itself and
    never appear here. See the model docstring for why that boundary is a column rather than a
    build step.
    """
    def course(entry):
        return {
            'step': entry['step'], 'level': entry['level'], 'key': entry['key'],
            'title': entry['title'], 'provider': entry.get('provider', ''), 'url': entry.get('url', ''),
            'desc': course_descriptions.get(entry['key'], ''),
        }

    def alternate(entry):
        return {
            'key': entry['key'], 'title': entry.get('title', ''), 'provider': entry.get('provider', ''),
            'url': entry.get('url', ''), 'desc': course_descriptions.get(entry['key'], ''),
        }

    return {
        'pathway': record['pathway'],
        'careers': record['careers_covered'],
        'mix': record['mix'],
        'family_description': record.get('family_description', ''),
        'careers_list': [
            {'name': c['name'], 'desc': c.get('desc', '')} for c in record.get('careers', [])
        ],
        'supply': record.get('supply', {}),
        'courses': [course(c) for c in record['courses']],
        'alt': {lv: [alternate(c) for c in record.get('alternates', {}).get(lv, [])] for lv in LEVELS},
    }


def parse_queue(raw):
    """
    Read a queue file's bytes or text into its records and course descriptions.

    Raises:
        QueueError: Not UTF-8, not JSON, not an object, or holding no ladders.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise QueueError(f'That file is not UTF-8 text: {exc}') from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise QueueError(f'That file is not valid JSON: {exc}') from exc
    if not isinstance(data, dict):
        raise QueueError('A queue file is a JSON object with a "ladders" list.')
    ladders = data.get('ladders')
    if not isinstance(ladders, list) or not ladders:
        raise QueueError('No "ladders" in the queue file; nothing to load.')
    descriptions = data.get('course_descriptions')
    return ladders, descriptions if isinstance(descriptions, dict) else {}


def validate(ladders):
    """
    Every reason these records could not be loaded, in the order they were found.

    Returns a list of strings, empty when the file is sound. Each names the record by position
    and id, because a generator writing the file needs to find the record, not the reviewer.
    """
    problems, seen = [], {}
    pools = set(ReviewPool.values)
    for index, record in enumerate(ladders, 1):
        if not isinstance(record, dict):
            problems.append(f'Record {index} is not an object.')
            continue
        name = f'Record {index} ({record.get("id") or "no id"})'
        missing = [field for field in REQUIRED_RECORD_FIELDS if record.get(field) in (None, '')]
        if missing:
            problems.append(f'{name} is missing {", ".join(missing)}.')
            continue
        item_id = str(record['id'])
        if len(item_id) > ITEM_ID_MAX_LENGTH:
            problems.append(f'{name} has an id longer than the column allows.')
        if item_id in seen:
            problems.append(f'{name} repeats the id of record {seen[item_id]}.')
        seen[item_id] = index
        if record['pool'] not in pools:
            problems.append(f'{name} has pool "{record["pool"]}"; expected one of {", ".join(sorted(pools))}.')
        courses = record['courses']
        if not isinstance(courses, list) or not courses:
            problems.append(f'{name} has no courses.')
            continue
        for position, course in enumerate(courses, 1):
            if not isinstance(course, dict):
                problems.append(f'{name} course {position} is not an object.')
                continue
            lacking = [field for field in REQUIRED_COURSE_FIELDS if course.get(field) in (None, '')]
            if lacking:
                problems.append(f'{name} course {position} is missing {", ".join(lacking)}.')
    return problems


def load_queue(ladders, course_descriptions, *, deactivate_missing=False, dry_run=False):
    """
    Upsert one :class:`PathwayReviewItem` per record, and report what changed.

    Args:
        deactivate_missing: Mark items absent from this file inactive. They are kept, not
            deleted, so the votes already cast on them stay readable.
        dry_run: Do all of it, report it, then roll back.

    Returns a dict with ``created``, ``updated``, ``deactivated``, ``deactivated_with_votes``,
    ``controls``, ``active``, ``item_ids`` and ``dry_run``.

    Raises:
        QueueError: The records do not validate. Nothing is written.
    """
    problems = validate(ladders)
    if problems:
        raise QueueError(f'{len(problems)} problem(s) in that queue file; nothing was loaded.', problems)

    with transaction.atomic():
        seen, created_count, updated_count = [], 0, 0
        for record in ladders:
            _, created = PathwayReviewItem.objects.update_or_create(
                item_id=record['id'],
                defaults={
                    'family_key': record['family_key'],
                    'pathway': record['pathway'],
                    'careers_covered': record['careers_covered'],
                    'mix': record['mix'],
                    'pool': record['pool'],
                    'stratum': record.get('stratum', ''),
                    'weight': record.get('weight') or 1.0,
                    'tier': 0 if record['pool'] in (ReviewPool.REACH, ReviewPool.CONTROL) else 1,
                    'is_active': True,
                    'payload': build_payload(record, course_descriptions),
                    'control_key': record.get('control_meta') or {},
                },
            )
            seen.append(str(record['id']))
            created_count += int(created)
            updated_count += int(not created)

        missing = PathwayReviewItem.objects.exclude(item_id__in=seen).filter(is_active=True)
        deactivated_with_votes = missing.filter(votes__isnull=False).distinct().count()
        deactivated = missing.count()
        if deactivate_missing:
            # Re-filtered rather than reusing the queryset above: counting it has not evaluated
            # it, and update() on a sliced or distinct queryset is refused.
            PathwayReviewItem.objects.exclude(item_id__in=seen).filter(is_active=True).update(is_active=False)
        else:
            deactivated = 0

        report = {
            'created': created_count,
            'updated': updated_count,
            'deactivated': deactivated,
            'deactivated_with_votes': deactivated_with_votes if deactivate_missing else 0,
            'would_deactivate': missing.count() if not deactivate_missing else 0,
            'controls': PathwayReviewItem.objects.filter(pool=ReviewPool.CONTROL, is_active=True).count(),
            'active': PathwayReviewItem.objects.filter(is_active=True).count(),
            'item_ids': seen,
            'dry_run': dry_run,
        }
        if dry_run:
            transaction.set_rollback(True)
    return report


def load_queue_file(raw, **kwargs):
    """:func:`parse_queue` then :func:`load_queue`, for a caller holding a file's contents."""
    ladders, descriptions = parse_queue(raw)
    return load_queue(ladders, descriptions, **kwargs)
