"""
The catalog's content-key vocabulary.

Domain knowledge, not harness knowledge: what a course key looks like, and why a course
*run* key is never one. Lives here so the evaluation harness can depend on the domain
rather than keeping a second copy of the rule -- see ``docs/architecture-patterns.md``
pattern 16, "Evaluation harnesses own no domain logic".
"""
import re

# Course keys as they appear in the Algolia catalog index's ``key`` field:
# "<org>+<course number>", e.g. "HarvardX+ER22.1x", "IBM+DA0101EN", "CodeSignal+34".
COURSE_KEY_PATTERN = re.compile(r'^[\w.\-]+\+[\w.\-]+$')

# A course *run* key. Valid elsewhere in the platform, and wrong wherever a course key
# belongs: the catalog index keys courses, so a run key can never match a hit.
COURSE_RUN_KEY_PREFIX = 'course-v1:'


def is_course_run_key(key: str) -> bool:
    """Whether ``key`` is a course *run* key rather than a course key."""
    return bool(key) and key.startswith(COURSE_RUN_KEY_PREFIX)


def is_valid_course_key(key: str) -> bool:
    """Whether ``key`` is a well-formed catalog course key."""
    return bool(key) and not is_course_run_key(key) and bool(COURSE_KEY_PATTERN.match(key))
