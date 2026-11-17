"""Tests for identifier redaction in learner messages (test-plan N-28)."""
from unittest import TestCase

import ddt

from enterprise_access.apps.personalized_search.core.redaction import redact


@ddt.ddt
class RedactTests(TestCase):
    """``redact`` replaces SSNs, card numbers and long digit runs, and leaves ordinary numbers alone."""

    @ddt.data(
        ('My SSN is 123-45-6789, can you help?', 'My SSN is [redacted], can you help?'),
        ('ssn 123 45 6789', 'ssn [redacted]'),
        ('ssn 123456789', 'ssn [redacted]'),
        ('card 4111 1111 1111 1111 please', 'card [redacted] please'),
        ('card 4111-1111-1111-1111', 'card [redacted]'),
        ('amex 3782 822463 10005', 'amex [redacted]'),
        ('card 4111111111111111', 'card [redacted]'),
        ('account 000123456789 at my bank', 'account [redacted] at my bank'),
        ('call me at 6175551234', 'call me at [redacted]'),  # a phone number as one run: redacted, documented
        ('123-45-6789 and 4111 1111 1111 1111', '[redacted] and [redacted]'),
    )
    @ddt.unpack
    def test_identifiers_are_redacted(self, text, expected):
        self.assertEqual(redact(text), expected)

    @ddt.data(
        'In 2026 I want 3 to 5 hours a week',
        'I finished CS50 and 6.00x last year',
        'course-v1:edX+DemoX+2T2026',
        'call me at 617-555-1234',  # a phone number with separators: not an identifier this guards against
        'a 12345678 eight-digit number',
        'grouped but not a card: 1234 5678 9012 3456',  # fails the Luhn check
        'version 1.2.3 and 10-12 weeks',
        '',
    )
    def test_ordinary_numbers_are_kept(self, text):
        self.assertEqual(redact(text), text)

    def test_redacting_twice_changes_nothing_more(self):
        once = redact('SSN 123-45-6789, card 4111 1111 1111 1111')
        self.assertEqual(redact(once), once)
