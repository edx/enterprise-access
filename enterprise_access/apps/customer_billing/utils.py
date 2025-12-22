"""
Utility functions for customer billing app
"""

import datetime

from django.utils import timezone

def datetime_from_timestamp(timestamp, tzinfo=None):
    naive_dt = datetime.datetime.fromtimestamp(timestamp)
    if tzinfo is None:
        tzinfo = timezone.get_current_timezone()
    return timezone.make_aware(naive_dt, tzinfo)
