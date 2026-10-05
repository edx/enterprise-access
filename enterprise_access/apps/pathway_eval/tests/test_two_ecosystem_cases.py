"""
Regression cases: the round-2 pathways that spanned two vendors' products must not come back.

Bench round 2 put 88 pathways in front of the reviewer, and pathways teaching two vendors' products
were the largest effect his votes and notes turned up ("too much mixed tech.. We have the courses,
its moer how it's assembled"). ``fixtures/two_ecosystem_cases`` holds every one of the 88 that
``pathways.ecosystems.candidate_ecosystems`` reads as spanning two -- its vendor-specific courses
share no ecosystem -- with each course's title, skill tags, provider and level, and, for each rung
holding a conflicting course, the alternates the reviewer was shown from that rung of the same
window. Every candidate carries its rank in that window, and the cases list them in that order.

Two things are checked for each:

* the detector still flags the pathway as reviewed as spanning two ecosystems, so a change to the
  patterns that stopped seeing the defect fails here rather than passing silently;
* selection with the one-ecosystem rule, over the stored candidates, builds a pathway that does not.
  ``single_ecosystem=True`` is passed explicitly, so the cases test the rule and not the default.

The cases were extracted from ``queue-round2.json``, ``blind_key.json`` and the collection and
replay checkpoints in the shape review; each records the sha256 of those files.
"""
import json
from pathlib import Path

import ddt
from django.test import SimpleTestCase

from enterprise_access.apps.pathways import pathway_variants
from enterprise_access.apps.pathways.ecosystems import EcosystemTracker, candidate_ecosystems
from enterprise_access.apps.pathways.pathway_assembly import Candidate

CASES_DIR = Path(__file__).resolve().parent.parent / 'fixtures' / 'two_ecosystem_cases'
CASE_IDS = sorted(path.stem for path in CASES_DIR.glob('*.json'))


def load_case(item_id) -> dict:
    return json.loads((CASES_DIR / f'{item_id}.json').read_text(encoding='utf-8'))


def spans_two(courses) -> bool:
    """Whether a pathway's vendor-specific courses share no ecosystem."""
    vendor = [ecosystems for ecosystems in map(candidate_ecosystems, courses) if ecosystems]
    return bool(vendor) and not frozenset.intersection(*vendor)


def candidates(case) -> list:
    """The stored window: the pathway's courses and the conflicting rungs' alternates, by rank."""
    pool = {course['key']: course for course in case['courses']}
    for alternates in case['alternates'].values():
        for course in alternates:
            pool.setdefault(course['key'], course)
    ordered = sorted(pool.values(), key=lambda course: course['window_rank'])
    return [
        Candidate(key=course['key'], title=course['title'], level_type=course['level_type'],
                  partner=course['partner'], language='English', skill_names=tuple(course['skill_names']))
        for course in ordered
    ]


@ddt.ddt
class TwoEcosystemCasesTests(SimpleTestCase):
    """Each case, the detector and the rule."""

    def test_the_cases_are_all_there(self):
        # 19 of round 2's 88 pathways read as spanning two; 14 of them were rated (the rest skipped).
        self.assertEqual(len(CASE_IDS), 19)
        self.assertEqual(sum(load_case(item_id)['reviewed'] for item_id in CASE_IDS), 14)

    @ddt.data(*CASE_IDS)
    def test_the_detector_flags_the_reviewed_pathway(self, item_id):
        case = load_case(item_id)
        self.assertTrue(spans_two(case['courses']), f'{item_id} no longer reads as spanning two ecosystems')
        tracker = EcosystemTracker(True)
        refused = []
        for course in case['courses']:
            if tracker.refuses(course):
                refused.append(course['key'])
            else:
                tracker.take(course)
        self.assertTrue(refused, f'{item_id}: the rule would have taken every course as reviewed')
        self.assertEqual(sorted({e for c in case['courses'] for e in candidate_ecosystems(c)}),
                         case['ecosystems'])

    @ddt.data(*CASE_IDS)
    def test_shape_cut_with_the_rule_keeps_one_ecosystem(self, item_id):
        case = load_case(item_id)
        shape = pathway_variants.parse_shape(case['shape'])
        variant = pathway_variants.shape_cut(candidates(case), shape, single_ecosystem=True)
        self.assertGreaterEqual(len(variant.courses), pathway_variants.MIN_PATHWAY_SIZE, item_id)
        self.assertFalse(spans_two(variant.courses), f'{item_id}: {[c.key for c in variant.courses]}')

    @ddt.data(*CASE_IDS)
    def test_ranked_cut_with_the_rule_keeps_one_ecosystem(self, item_id):
        case = load_case(item_id)
        variant = pathway_variants.ranked_cut(candidates(case), len(case['courses']), single_ecosystem=True)
        self.assertGreaterEqual(len(variant.courses), pathway_variants.MIN_PATHWAY_SIZE, item_id)
        self.assertFalse(spans_two(variant.courses), f'{item_id}: {[c.key for c in variant.courses]}')

    def test_the_cases_can_fail(self):
        # Without the rule, the same windows rebuild the defect in most cases -- so the passes above
        # are the rule's doing, not something the stored candidates guarantee.
        spanning = [
            item_id for item_id in CASE_IDS
            if spans_two(pathway_variants.shape_cut(
                candidates(load_case(item_id)), pathway_variants.parse_shape(load_case(item_id)['shape']),
                single_ecosystem=False,
            ).courses)
        ]
        self.assertGreater(len(spanning), len(CASE_IDS) // 2)
