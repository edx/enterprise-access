"""
API file interacting with assignment metadata (created to avoid a circular
import error)
"""
import datetime
import logging

from django.utils import timezone
from edx_django_utils.cache import TieredCache
from requests.exceptions import HTTPError

from enterprise_access.apps.content_metadata.api import (
    get_and_cache_catalog_content_metadata,
    get_and_cache_content_metadata
)
from enterprise_access.cache_utils import versioned_cache_key

logger = logging.getLogger(__name__)

DATE_INPUT_PATTERNS = [
    '%Y-%m-%dT%H:%M:%SZ',
    '%Y-%m-%dT%H:%M:%S.%fZ',
    '%Y-%m-%d %H:%M:%SZ',
    '%Y-%m-%d %H:%M:%S.%fZ',
]
DEFAULT_STRFTIME_PATTERN = '%b %d, %Y'
# How long a catalog-agnostic lookup miss is remembered, so that many assignments for the same content
# don't each repeat the call in one nightly run.
CATALOG_AGNOSTIC_MISS_CACHE_TIMEOUT = 60 * 60


def _content_metadata_for_assignment(assignment, course_metadata_list):
    """
    Given a list of course metadata dictionaries and an assignment,
    find the course metadata dictionary that corresponds to the
    assignment's content_key (course run) or parent_content_key (course).
    """
    return next(
        (
            course_metadata
            for course_metadata in course_metadata_list
            if course_metadata.get('key') in (assignment.content_key, assignment.parent_content_key)
        ),
        None
    )


def get_content_metadata_for_assignments(enterprise_catalog_uuid, assignments):
    """
    Fetches (from cache or enterprise-catalog API call) content metadata
    in bulk for the `content_keys` of the given assignments, provided
    such metadata is related to the given `enterprise_catalog_uuid`.

    Note that the `content_keys` of the provided assignments may be
    either course run keys or course keys. Regardless of the type of key,
    the content metadata API will return the metadata at the course-level.

    Returns:
        A dict mapping every content key of the provided assignments
        to a content metadata dictionary, or null if no such dictionary
        could be found for a given key.
    """
    content_keys = {assignment.content_key for assignment in assignments}
    course_metadata_list = get_and_cache_catalog_content_metadata(enterprise_catalog_uuid, content_keys)
    metadata_by_key = {
        assignment.content_key: _content_metadata_for_assignment(assignment, course_metadata_list)
        for assignment in assignments
    }
    return metadata_by_key


def get_catalog_agnostic_content_metadata_for_assignment(assignment):
    """
    Fetch content metadata without relying on the assignment's policy catalog.

    Tries the assignment's content key, then its parent content key, each with
    ``coerce_to_parent_course=True`` so that run keys resolve to the parent course (which carries the
    per-run data). Misses are not cached by ``get_and_cache_content_metadata``, so this should only be
    called from the automatic-expiration path.

    Returns:
        dict: content metadata, or an empty dict if nothing could be fetched.
    """
    content_identifiers = []
    if assignment.content_key:
        content_identifiers.append(assignment.content_key)
    if assignment.parent_content_key and assignment.parent_content_key not in content_identifiers:
        content_identifiers.append(assignment.parent_content_key)

    for content_identifier in content_identifiers:
        miss_cache_key = versioned_cache_key('catalog_agnostic_content_metadata_miss', content_identifier)
        if TieredCache.get_cached_response(miss_cache_key).is_found:
            continue

        try:
            content_metadata = get_and_cache_content_metadata(
                content_identifier,
                coerce_to_parent_course=True,
            )
        except HTTPError as exc:  # connection errors and timeouts are retried by the catalog client
            logger.warning(
                'Could not fetch catalog-agnostic content metadata for %s: %s',
                content_identifier,
                exc,
            )
            content_metadata = None
        if content_metadata:
            return content_metadata

        TieredCache.set_all_tiers(
            miss_cache_key, True, django_cache_timeout=CATALOG_AGNOSTIC_MISS_CACHE_TIMEOUT,
        )

    return {}


def get_run_dates(content_metadata, date_field):
    """
    Parse ``date_field`` (e.g. ``enroll_by_date`` or ``end_date``) from every run in a content metadata
    record's ``normalized_metadata_by_run``.

    Returns:
        list: one UTC datetime per run, in run order, with None for runs that lack the field.
            Empty if there are no runs. Raises ValueError if a present value can't be parsed.
    """
    runs_metadata = (content_metadata or {}).get('normalized_metadata_by_run') or {}
    return [
        parse_datetime_string(run_metadata.get(date_field), set_to_utc=True)
        for run_metadata in runs_metadata.values()
    ]


def get_card_image_url(content_metadata):
    """
    Helper to get the appropriate course card image
    from a content metadata dictionary.
    """
    if first_choice := content_metadata.get('card_image_url'):
        return first_choice
    if second_choice := content_metadata.get('image_url'):
        return second_choice
    return None


def get_human_readable_date(datetime_string, output_pattern=DEFAULT_STRFTIME_PATTERN):
    """
    Given a datetime string value from some content metadata record,
    convert it to the provided pattern.
    """
    datetime_obj = parse_datetime_string(datetime_string)
    if datetime_obj:
        return datetime_obj.strftime(output_pattern)
    return None


def parse_datetime_string(datetime_string, set_to_utc=False):
    """
    Given a datetime string value from some content metadata record,
    parse it into a datetime object.
    """
    if not datetime_string:
        return None

    last_exception = None
    for input_pattern in DATE_INPUT_PATTERNS:
        try:
            formatted_date = datetime.datetime.strptime(datetime_string, input_pattern)
            if set_to_utc:
                return formatted_date.replace(tzinfo=datetime.timezone.utc)
            return formatted_date
        except ValueError as exc:
            last_exception = exc

    if last_exception is not None:
        raise last_exception
    return None


def get_course_partners(course_metadata):
    """
    Returns a list of course partner data for subsidy requests given a course dictionary.
    """
    owners = course_metadata.get('owners') or []
    names = [owner.get('name') for owner in owners]
    if len(names) < 1:
        raise Exception('Course must have a partner')
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return ' and '.join(names)
    return ', '.join(names[:-1]) + ', and ' + names[-1]


def is_date_n_days_from_now(target_datetime, num_days):
    """
    Determine if the target_datetime is exactly num_days from the current
    UTC date and time.

        Args:
            target_datetime (datetime): A datetime object in UTC that is to be compared.
            num_days (int): The number of days from the current date to check against the
                        target datetime

        Returns:
            bool: True if target_datetime is num_days away from now, otherwise False.
    """
    future_datetime = timezone.now() + timezone.timedelta(days=num_days)
    return target_datetime.date() == future_datetime.date()


def get_normalized_metadata_for_assignment(assignment, content_metadata):
    """
    Retrieve normalized metadata for a given assignment. If the assignment has
    a preferred course run key, return the metadata for that run.
    If metadata for the run is missing, return the normalized metadata for the advertised run.

    Args:
        assignment: The assignment object.
        content_metadata (dict): The content metadata object

    Returns:
        dict: Normalized metadata, either for a specific course run or the advertised course run.
    """
    normalized_metadata_by_run = content_metadata.get('normalized_metadata_by_run', {})
    # Return the content metadata for a specific course run based on the preferred_course_run_key
    if preferred_course_run_key := assignment.preferred_course_run_key:
        return normalized_metadata_by_run.get(preferred_course_run_key, {})
    # Return current advertised course run metadata if preferred_course_run_key is NULL (i.e.,
    # impacting legacy course-based assignments created pre-May 2024).
    return content_metadata.get('normalized_metadata', {})
