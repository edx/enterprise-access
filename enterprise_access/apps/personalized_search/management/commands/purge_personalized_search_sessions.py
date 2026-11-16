"""
Permanently delete Personalized Search sessions with no message in the last ``--days`` days (default 30).

Scheduled daily (a Kubernetes cron job in the enterprise-access stage and prod config). Each session goes through
``delete_conversation``, the same delete as the learner's X: its turns go, and the learner's saved and
not-interested items stay. Soft-deleted sessions are purged too. See ``docs/references/personalized_search.md``.
"""
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from enterprise_access.apps.personalized_search import events
from enterprise_access.apps.personalized_search.adapters.django_store import (
    delete_conversation,
    sessions_inactive_since
)
from enterprise_access.apps.personalized_search.models import ChatSession

logger = logging.getLogger(__name__)
LOG_PREFIX = '[PURGE_PERSONALIZED_SEARCH_SESSIONS]'


class Command(BaseCommand):
    """Delete Personalized Search sessions inactive for longer than the retention window."""

    help = 'Permanently delete Personalized Search sessions with no message in the last --days days.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30,
                            help='Delete sessions whose last message is older than this many days (default 30).')
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help='Log how many sessions would be deleted, and delete nothing.')
        parser.add_argument('--batch-size', type=int, default=100,
                            help='Sessions loaded per query (default 100).')

    def handle(self, *args, **options):
        days, dry_run, batch_size = options['days'], options['dry_run'], options['batch_size']
        if days < 1 or batch_size < 1:
            raise CommandError('--days and --batch-size must be at least 1.')
        cutoff = timezone.now() - timedelta(days=days)
        # Frozen up front: the deletes below shrink the set, so it must not be paged with offsets.
        uuids = sessions_inactive_since(cutoff)
        logger.info('%s dry_run=%s days=%s cutoff=%s sessions=%s', LOG_PREFIX, dry_run, days, cutoff.isoformat(),
                    len(uuids))
        if dry_run:
            return

        deleted, failed = 0, []
        for start in range(0, len(uuids), batch_size):
            for session in ChatSession.all_objects.filter(uuid__in=uuids[start:start + batch_size]):
                try:
                    delete_conversation(session, reason=events.DELETED_FOR_RETENTION)
                    deleted += 1
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.exception('%s could not delete session %s', LOG_PREFIX, session.uuid)
                    failed.append(str(session.uuid))
        logger.info('%s deleted=%s failed=%s', LOG_PREFIX, deleted, len(failed))
        if failed:
            raise CommandError(f'{len(failed)} session(s) could not be deleted: {", ".join(failed)}')
