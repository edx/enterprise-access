"""
Identifier redaction (test-plan N-28): a learner who types an SSN, a card number or an account number into the chat
shouldn't have it sent to a model or stored. The turn endpoint redacts the message once, before the session's
``first_prompt`` and title, the stored turn, the history and the model calls see it.

What counts, and what deliberately doesn't:
- US SSNs written with dashes or spaces (``123-45-6789``, ``123 45 6789``).
- Card numbers grouped with spaces or dashes (4-4-4-4 and its 13- to 19-digit variants, Amex 4-6-5) that pass the
  Luhn check. Ungrouped, a card number is a digit run (below).
- Any other unbroken run of 9 or more digits (account, routing or member numbers; a 9-digit SSN). This also redacts
  a phone number written as one run (``6175551234``), which is fine: the chat never needs one.
- Not redacted: years, durations and course numbers ("2026", "3 hours", "CS50", "6.00x"), and phone numbers written
  with separators ("617-555-1234"), which aren't the identifiers this guards against.
"""
import re

PLACEHOLDER = '[redacted]'
SSN = re.compile(r'(?<![\w-])\d{3}([- ])\d{2}\1\d{4}(?![\w-])')
CARD = re.compile(r'(?<![\w-])(?:\d{4}([ -])\d{4}\1\d{4}\1\d{1,4}(?:\1\d{1,3})?|\d{4}([ -])\d{6}\2\d{5})(?![\w-])')
DIGIT_RUN = re.compile(r'(?<!\w)\d{9,}(?!\w)')


def _luhn(digits: str) -> bool:
    """Whether ``digits`` pass the Luhn checksum every card number carries."""
    total = 0
    for i, char in enumerate(reversed(digits)):
        value = int(char) * (2 if i % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def _card(match: re.Match) -> str:
    """A grouped number that is a card number, redacted; anything else as it was."""
    digits = re.sub(r'\D', '', match.group())
    return PLACEHOLDER if 13 <= len(digits) <= 19 and _luhn(digits) else match.group()


def redact(text: str) -> str:
    """``text`` with SSNs, card numbers and long digit runs replaced by ``[redacted]``."""
    if not text:
        return text
    text = CARD.sub(_card, text)
    text = SSN.sub(PLACEHOLDER, text)
    return DIGIT_RUN.sub(PLACEHOLDER, text)
