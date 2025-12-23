"""
Utility functions for customer billing app
"""

import datetime

from django.utils import timezone


def datetime_from_timestamp(timestamp):
    """
    Convert a timestamp to a timezone-aware datetime.
    """
    naive_dt = datetime.datetime.fromtimestamp(timestamp)
    return timezone.make_aware(naive_dt)
