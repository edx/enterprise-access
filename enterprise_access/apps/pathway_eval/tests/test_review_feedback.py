"""
Tests for the shape review's offline evaluation and replay harness.

The properties that matter: votes read the same whichever swap shape the bench wrote; the
fixture is derived deterministically, with ranks taken from the career's own window and
skipped careers kept out of every measure; themes are coded by the frozen keyword table,
with overrides as the only escape hatch; a replay calls the app's own selection and judge on
the stored window, in a shape ``select_shapes`` reads; and scoring and the judge-swap check
count what they should, within their call budget.
"""
import inspect
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ddt
from django.test import TestCase

from enterprise_access.apps.pathway_eval import review_feedback, shape_review
from enterprise_access.apps.pathway_eval.review_feedback import (
    ReplayContractError,
    append_replay,
    build_fixture,
    career_context_from_queue,
    career_inputs_from_replays,
    code_themes,
    judge_prefers_replacement,
    judge_swap_call_bound,
    judgement_rank,
    load_replays,
    load_votes,
    normalise_swap,
    normalise_vote,
    replay_call_bound,
    replay_career,
    score_swaps,
    vote_themes
)
from enterprise_access.apps.pathway_eval.variant_collection import CareerRun
from enterprise_access.apps.pathways import pathway_variants
from enterprise_access.apps.pathways.model_backends.base import ModelResponse
from enterprise_access.apps.pathways.pathway_assembly import Candidate
from enterprise_access.apps.pathways.pathway_variants import Variant

LEVELS = ('Introductory', 'Intermediate', 'Advanced')
ALPHA, BETA = 'Alpha Analyst', 'Beta Manager'


def window_course(key, level, title, partner):
    """One candidate as the collection records it in the window."""
    return {
        'key': key, 'title': title, 'level_type': level, 'partner': partner, 'language': 'English',
        'short_description': f'About {title}.', 'full_description': '', 'skill_names': ['Alpha'],
    }


# Alpha Analyst's window, in re-rank order. Introductory rung ranks: I1 0, I2 1, I3 2, I4 3.
WINDOW = [
    window_course('AlphaX+I1', 'Introductory', 'Alpha Basics', 'Alpha University'),
    window_course('AlphaX+M1', 'Intermediate', 'Alpha Methods', 'Beta University'),
    window_course('RuriX+I2', 'Introductory', 'Alpha in Ruritania', 'Bank of Ruritania'),
    window_course('AlphaX+A1', 'Advanced', 'Alpha Mastery', 'Gamma University'),
    window_course('GenX+I3', 'Introductory', 'Generative AI for Analysts', 'Delta University'),
    window_course('AlphaX+M2', 'Intermediate', 'Alpha Practice', 'Epsilon University'),
    window_course('CommX+I4', 'Introductory', 'Alpha Communication', 'Zeta University'),
    window_course('AlphaX+A2', 'Advanced', 'Alpha Strategy', 'Eta University'),
]
BETA_WINDOW = [
    window_course('BetaX+B1', 'Introductory', 'Beta Basics', 'Beta University'),
    window_course('BetaX+B2', 'Introductory', 'Beta Budgets', 'Gamma University'),
]
BY_KEY = {course['key']: course for course in WINDOW + BETA_WINDOW}


def queue_item(item_id, career, tier_label, mix, keys, *, family=('Junior {career}',)):
    """A bench queue item: the pathway's courses by step, and the career family."""
    return {
        'id': item_id, 'pathway': f'{career} · {tier_label}', 'mix': mix,
        'courses': [
            {'step': step, 'key': key, 'title': BY_KEY[key]['title'], 'level': BY_KEY[key]['level_type'],
             'provider': BY_KEY[key]['partner']}
            for step, key in enumerate(keys, 1)
        ],
        'alternates': {'Introductory': [{'key': 'GenX+I3', 'title': 'Generative AI for Analysts'}]},
        'careers': [{'name': career, 'desc': ''}] + [{'name': title.format(career=career), 'desc': ''}
                                                     for title in family],
        'careers_covered': 1 + len(family),
        'family_description': f'{career}s do {career.split()[0].lower()} work.',
    }


def make_queue():
    return {'ladders': [
        queue_item('S01-1-intro', ALPHA, 'Introductory, 2 courses', '2/0/0', ['AlphaX+I1', 'RuriX+I2']),
        queue_item('S01-2-inter', ALPHA, 'Intermediate, 2 courses', '0/2/0', ['AlphaX+M1', 'AlphaX+M2']),
        queue_item('S01-3-ladder', ALPHA, 'Full ladder', '1/1/1', ['AlphaX+I1', 'AlphaX+M1', 'AlphaX+A1']),
        queue_item('S02-1-intro', BETA, 'Introductory, 2 courses', '2/0/0', ['BetaX+B1', 'BetaX+B2']),
    ]}


def make_judge_key():
    def item(career, tier, verdict, on_topic):
        return {'career': career, 'tier': tier, 'verdict': verdict, 'on_topic': on_topic}
    return {'items': {
        'S01-1-intro': item(ALPHA, 'intro', 'good', {'AlphaX+I1': True, 'RuriX+I2': True}),
        'S01-2-inter': item(ALPHA, 'intermediate', 'good', {}),
        'S01-3-ladder': item(ALPHA, 'ladder', 'weak', {'AlphaX+I1': True, 'AlphaX+M1': True, 'AlphaX+A1': False}),
        'S02-1-intro': item(BETA, 'intro', 'good', {}),
    }}


def make_runs():
    """The collection's runs, as a checkpoint holds them."""
    def run(career, candidates):
        return CareerRun(requested_name=career, career_name=career, workflow_uuid=f'wf-{career}',
                         pathway={'courses': [], 'complete': True}, candidates=list(candidates)).to_dict()
    return [run(ALPHA, WINDOW), run(BETA, BETA_WINDOW)]


def make_votes():
    """Bench votes as exported: string steps, both swap shapes, all three outcomes, a skip-only career."""
    return [
        {'item_id': 'S01-1-intro', 'verdict': 'needs_work', 'drops': [2], 'swaps': {'2': 'GenX+I3'},
         'notes': 'Alpha in Ruritania should be localized for its own market.', 'reasons': [],
         'created': '2026-09-28 10:00:00'},
        {'item_id': 'S01-2-inter', 'verdict': 'good', 'drops': [], 'swaps': {}, 'notes': '', 'reasons': [],
         'created': '2026-09-28 10:01:00'},
        {'item_id': 'S01-3-ladder', 'verdict': 'needs_work', 'drops': [1, 2, 3],
         'swaps': {'1': {'best': 'CommX+I4', 'also': ['GenX+I3']}, '2': '', '3': '__none__'},
         'notes': 'The first course overlaps the second.', 'reasons': [], 'created': '2026-09-28 10:02:00'},
        {'item_id': 'S02-1-intro', 'verdict': 'skip', 'drops': [2], 'swaps': {'2': ''},
         'notes': 'Hard to judge.', 'reasons': [], 'created': '2026-09-28 10:03:00'},
    ]


def make_fixture(**kwargs):
    return build_fixture(votes=make_votes(), queue=make_queue(), judge_key=make_judge_key(),
                         checkpoint_runs=make_runs(), **kwargs)


def pathway_dict(label, keys, *, verdict='good', rubric_field='judgement'):
    """A judged, complete variant as an exported run carries it."""
    mix = {level: sum(1 for key in keys if BY_KEY[key]['level_type'] == level) for level in LEVELS}
    return {
        'label': label, 'strategy': label.partition(':')[0], 'shape': label.partition(':')[2],
        'courses': [{'key': key, 'title': BY_KEY[key]['title'], 'level_type': BY_KEY[key]['level_type']}
                    for key in keys],
        'complete': True, 'level_mix': mix, 'violations': [], 'error': '',
        rubric_field: {'verdict': verdict, 'reason': '', 'n_on_topic': len(keys), 'n_courses': len(keys),
                       'on_topic': {key: True for key in keys}, 'error': '', 'flags': {}},
    }


def replay_run(career, variants):
    return {'requested_name': career, 'career_name': career, 'workflow_uuid': f'replay:{career}',
            'pathway': None, 'judgement': None, 'variants': variants, 'candidates': WINDOW, 'error': '',
            'skipped_reason': '', 'replay': {'career_skills': ['alpha', 'analysis'], 'career_description': 'desc',
                                             'family_titles': ['Junior Alpha Analyst'], 'family_size': 2}}


def candidate(key):
    course = BY_KEY[key]
    return Candidate(key=key, title=course['title'], level_type=course['level_type'], partner=course['partner'],
                     language='English')


TRACE = {'backend': 'fake', 'model': 'fake-1', 'input_tokens': 10, 'output_tokens': 5, 'elapsed_ms': 3}


def fake_judgement(**kwargs):
    """Stands in for ``judging.judge_pathway``: every course on topic, good."""
    keys = [course['key'] for course in kwargs['courses']]
    return {
        'verdict': 'good', 'reason': 'fits', 'on_topic': {key: True for key in keys}, 'fabricated_keys': [],
        'unjudged_keys': [], 'error': '', 'n_on_topic': len(keys), 'n_courses': len(keys), 'trace': dict(TRACE),
        'rubric': kwargs.get('rubric', 'v1'), 'flags': {},
    }


class TestVoteNormalisation(TestCase):
    """
    Scenario: A dropped step is a pick, "nothing would work" or no pick, whichever shape the
    bench wrote it in.
    """

    PICK = {'outcome': 'pick', 'best': 'HarvardX+CS50P', 'also': [], 'none': False}
    NOTHING = {'outcome': 'nothing_works', 'best': None, 'also': [], 'none': True}
    NO_PICK = {'outcome': 'no_pick', 'best': None, 'also': [], 'none': False}

    def test_a_legacy_string_is_the_best_replacement(self):
        self.assertEqual(normalise_swap('HarvardX+CS50P'), self.PICK)
        self.assertEqual(normalise_swap({'best': 'HarvardX+CS50P'}), self.PICK)

    def test_only_the_none_marker_means_nothing_would_work(self):
        for value in ('__none__', {'best': '__none__', 'also': []}, {'best': '', 'none': True}):
            self.assertEqual(normalise_swap(value), self.NOTHING, value)

    def test_an_empty_string_is_a_drop_without_a_pick_not_a_gap(self):
        for value in ('', '  ', None, {'best': '', 'also': []}, {}):
            self.assertEqual(normalise_swap(value), self.NO_PICK, value)

    def test_the_new_shape_keeps_best_and_deduplicated_alternatives(self):
        swap = normalise_swap({'best': 'A+1', 'also': ['B+2', 'A+1', '', 'B+2', '__none__', 'C+3']})
        self.assertEqual(swap, {'outcome': 'pick', 'best': 'A+1', 'also': ['B+2', 'C+3'], 'none': False})

    def test_alternatives_without_a_best_are_still_a_pick(self):
        self.assertEqual(normalise_swap({'best': '', 'also': ['B+2']}),
                         {'outcome': 'pick', 'best': None, 'also': ['B+2'], 'none': False})

    def test_normalising_is_idempotent(self):
        for value in ('A+1', '', '__none__', {'best': 'A+1', 'also': ['B+2']}):
            once = normalise_swap(value)
            self.assertEqual(normalise_swap(once), once)

    def test_a_vote_gets_int_steps_and_every_dropped_step_an_outcome(self):
        vote = normalise_vote({'item_id': 'S01-1-intro', 'verdict': 'needs_work', 'drops': ['2', 3],
                               'swaps': {'1': 'A+1', '2': ''}})
        self.assertEqual(vote['drops'], [1, 2, 3])
        self.assertEqual({step: swap['outcome'] for step, swap in vote['swaps'].items()},
                         {1: 'pick', 2: 'no_pick', 3: 'no_pick'})
        self.assertEqual(normalise_vote(vote), vote)

    def test_load_votes_reads_an_export_or_a_bare_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            for payload in ({'votes': make_votes(), 'reviewer': 'x'}, make_votes()):
                path = Path(tmp) / 'votes.json'
                path.write_text(json.dumps(payload))
                votes = load_votes(path)
                self.assertEqual([vote['item_id'] for vote in votes],
                                 ['S01-1-intro', 'S01-2-inter', 'S01-3-ladder', 'S02-1-intro'])
                self.assertEqual(votes[2]['swaps'][1],
                                 {'outcome': 'pick', 'best': 'CommX+I4', 'also': ['GenX+I3'], 'none': False})
                self.assertEqual(votes[2]['swaps'][2]['outcome'], 'no_pick')
                self.assertEqual(votes[2]['swaps'][3]['outcome'], 'nothing_works')


@ddt.ddt
class TestThemes(TestCase):
    """
    Scenario: Notes are coded by the frozen keyword table, in priority order, with overrides.
    """

    @ddt.data(
        ('we should localize these courses for not the english market.', ['regional']),
        ('keep stuff like this more regional', ['regional']),
        ('We should always favor cs 50 for introductory. For other choice chose AI', ['flagship_coherence',
                                                                                      'promote_ai']),
        ('since we push CS 50 at the beginner level we should slightly favor python for higher levels.',
         ['flagship_coherence']),
        ('would prefer we suggest a little more AI related courses', ['promote_ai']),
        ('A CRM course seems too specific for the broad family.', ['too_specific']),
        ('just seems overly tech specific for the family level.', ['too_specific']),
        ('The 2 beginners courses seemed to similar.', ['redundant']),
        ('The 2 courses seem very close in their coverage.', ['redundant']),
        ('Coaching is a better pick for a manager.', ['role_fit']),
        ('Specialist implies working with stakeholders.', ['role_fit']),
        ('This is good but very hard core/challenging.', ['level_honesty']),
        ('I would prefer an actual introductory course in here.', ['level_honesty']),
        ('She said it was fine.', []),
        ('', []),
    )
    @ddt.unpack
    def test_each_note_codes_to_its_themes(self, notes, themes):
        self.assertEqual(code_themes(notes), themes)

    def test_reason_chips_code_only_a_note_that_says_nothing_codeable(self):
        self.assertEqual(code_themes('', ['wrong_level']), ['level_honesty'])
        self.assertEqual(code_themes('Too specific.', ['wrong_job']), ['too_specific'])
        self.assertEqual(code_themes('', ['too_generic']), [])

    def test_a_silent_vote_has_no_theme_and_an_unexplained_edit_is_other(self):
        silent = normalise_vote({'item_id': 'S01-1-intro', 'verdict': 'good'})
        self.assertEqual(vote_themes(silent), ([], 'none'))
        edited = normalise_vote({'item_id': 'S01-1-intro', 'verdict': 'needs_work', 'drops': [1]})
        self.assertEqual(vote_themes(edited), (['other'], 'other'))

    def test_the_sentence_naming_a_dropped_course_decides_its_swap(self):
        fixture = make_fixture()
        pair = fixture['swap_pairs'][0]
        self.assertEqual((pair['item_id'], pair['theme'], pair['theme_source']),
                         ('S01-1-intro', 'regional', 'sentence'))

    def test_promote_ai_goes_to_the_ai_replacement_and_not_to_its_sibling(self):
        vote = normalise_vote({
            'item_id': 'S01-3-ladder', 'verdict': 'needs_work', 'drops': [1, 2],
            'swaps': {'1': 'GenX+I3', '2': 'AlphaX+M2'},
            'notes': 'The two courses overlap. Moved an AI course in as we want to promote that content.',
        })
        themes, _ = vote_themes(vote)
        self.assertEqual(themes, ['promote_ai', 'redundant'])
        dropped = {'key': 'AlphaX+M1', 'title': 'Alpha Methods', 'provider': 'Beta University'}
        self.assertEqual(review_feedback.swap_theme(vote, 1, dropped, 'Generative AI for Analysts', themes=themes),
                         ('promote_ai', 'ai_replacement'))
        self.assertEqual(review_feedback.swap_theme(vote, 2, dropped, 'Alpha Practice', themes=themes),
                         ('redundant', 'vote'))

    def test_overrides_recode_a_vote_or_one_swap(self):
        fixture = make_fixture(theme_overrides={'S01-1-intro': 'role_fit', ('S01-3-ladder', 1): 'level_honesty'})
        pairs = {(pair['item_id'], pair['replacement_key']): pair for pair in fixture['swap_pairs']}
        self.assertEqual(pairs[('S01-1-intro', 'GenX+I3')]['theme'], 'role_fit')
        self.assertEqual(pairs[('S01-3-ladder', 'CommX+I4')]['theme'], 'level_honesty')
        votes = {vote['item_id']: vote for vote in fixture['votes']}
        self.assertEqual((votes['S01-1-intro']['themes'], votes['S01-1-intro']['theme_source']),
                         (['role_fit'], 'override'))
        self.assertEqual(fixture['themes']['overrides'],
                         {'S01-1-intro': 'role_fit', 'S01-3-ladder#1': 'level_honesty'})


class TestBuildFixture(TestCase):
    """
    Scenario: The fixture is derived once, offline, from votes, queue, judge key and windows.
    """

    def setUp(self):
        super().setUp()
        self.fixture = make_fixture()

    def test_one_swap_pair_per_chosen_replacement_with_window_and_rung_ranks(self):
        pairs = [(p['item_id'], p['step'], p['dropped_key'], p['replacement_key'], p['is_best'])
                 for p in self.fixture['swap_pairs']]
        self.assertEqual(pairs, [
            ('S01-1-intro', 2, 'RuriX+I2', 'GenX+I3', True),
            ('S01-3-ladder', 1, 'AlphaX+I1', 'CommX+I4', True),
            ('S01-3-ladder', 1, 'AlphaX+I1', 'GenX+I3', False),
        ])
        first = self.fixture['swap_pairs'][0]
        self.assertEqual(
            {key: first[key] for key in ('dropped_window_rank', 'replacement_window_rank', 'dropped_rung_rank',
                                         'replacement_rung_rank', 'rung_size', 'window_size')},
            {'dropped_window_rank': 2, 'replacement_window_rank': 4, 'dropped_rung_rank': 1,
             'replacement_rung_rank': 2, 'rung_size': 4, 'window_size': 8},
        )
        self.assertEqual((first['career'], first['tier'], first['level']), (ALPHA, 'intro', 'Introductory'))
        self.assertEqual((first['dropped_title'], first['replacement_title']),
                         ('Alpha in Ruritania', 'Generative AI for Analysts'))
        self.assertTrue(first['dropped_judged_on_topic'])

    def test_nothing_would_work_and_kept_good_are_recorded(self):
        self.assertEqual(
            [(e['item_id'], e['step'], e['level'], e['dropped_key']) for e in self.fixture['nothing_would_work']],
            [('S01-3-ladder', 3, 'Advanced', 'AlphaX+A1')],
        )
        self.assertEqual(
            [(e['item_id'], e['tier'], e['kept_keys']) for e in self.fixture['kept_good']],
            [('S01-2-inter', 'intermediate', ['AlphaX+M1', 'AlphaX+M2'])],
        )

    def test_a_drop_without_a_pick_is_listed_apart_and_never_as_a_gap(self):
        self.assertEqual(
            [(e['item_id'], e['step'], e['dropped_key'], e['scored']) for e in self.fixture['dropped_without_pick']],
            [('S01-3-ladder', 2, 'AlphaX+M1', True), ('S02-1-intro', 2, 'BetaX+B2', False)],
        )
        gaps = {(entry['item_id'], entry['step']) for entry in self.fixture['nothing_would_work']}
        self.assertNotIn(('S01-3-ladder', 2), gaps)
        self.assertEqual((self.fixture['summary']['dropped_without_pick'],
                          self.fixture['summary']['dropped_without_pick_scored']), (2, 1))
        edits = {vote['item_id']: vote['edits'] for vote in self.fixture['votes']}
        self.assertEqual([edit['outcome'] for edit in edits['S01-3-ladder']], ['pick', 'no_pick', 'nothing_works'])

    def test_a_career_skipped_throughout_is_excluded_and_its_edits_ignored(self):
        self.assertEqual(self.fixture['excluded_careers'], [BETA])
        self.assertFalse(any(entry['career'] == BETA for entry in self.fixture['nothing_would_work']))
        votes = {vote['item_id']: vote for vote in self.fixture['votes']}
        self.assertFalse(votes['S02-1-intro']['scored'])

    def test_the_confusion_keeps_skips_apart_and_reports_judge_good_precision(self):
        confusion = self.fixture['confusion']
        self.assertEqual(confusion['matrix']['good'], {'good': 1, 'needs_work': 1, 'bad': 0})
        self.assertEqual(confusion['matrix']['weak'], {'good': 0, 'needs_work': 1, 'bad': 0})
        self.assertEqual(confusion['skips'], {'good': 1, 'weak': 0, 'bad': 0})
        self.assertEqual(confusion['judge_good_precision'], {'value': 0.5, 'numerator': 1, 'denominator': 2})
        self.assertEqual(confusion['scored'], 3)

    def test_rank_stats_count_the_replacements_the_ranker_placed_lower(self):
        stats = self.fixture['rank_stats']
        self.assertEqual((stats['pairs'], stats['unique_pairs'], stats['replacement_below_dropped_on_rung']),
                         (2, 2, 2))
        self.assertEqual(stats['rung_rank_delta']['median'], 2)
        self.assertEqual(stats['replacement_not_in_window'], 0)

    def test_theme_counts_cover_scored_votes_and_best_swaps(self):
        self.assertEqual(self.fixture['themes']['vote_counts'], {'regional': 1, 'redundant': 1})
        self.assertEqual(self.fixture['themes']['swap_counts'], {'regional': 1, 'redundant': 1})

    def test_the_fixture_is_json_safe_and_stable(self):
        again = build_fixture(votes=list(reversed(make_votes())), queue=make_queue(), judge_key=make_judge_key(),
                              checkpoint_runs=list(reversed(make_runs())))
        self.assertEqual(json.dumps(self.fixture, sort_keys=True), json.dumps(again, sort_keys=True))

    def test_the_latest_vote_on_an_item_wins(self):
        votes = make_votes() + [{'item_id': 'S01-1-intro', 'verdict': 'good', 'created': '2026-09-29 09:00:00'}]
        fixture = build_fixture(votes=votes, queue=make_queue(), judge_key=make_judge_key(),
                                checkpoint_runs=make_runs())
        self.assertEqual(fixture['summary']['superseded_votes'], 1)
        self.assertEqual(fixture['items']['S01-1-intro']['human_verdict'], 'good')
        self.assertNotIn('S01-1-intro', {pair['item_id'] for pair in fixture['swap_pairs']})

    def test_careers_are_cross_checked_against_the_careers_file(self):
        make_fixture(career_names=[ALPHA, BETA])
        with self.assertRaisesRegex(ValueError, 'disagree'):
            make_fixture(career_names=[BETA, ALPHA])

    def test_a_vote_for_an_item_not_in_the_queue_is_refused(self):
        votes = make_votes() + [{'item_id': 'S09-1-intro', 'verdict': 'good'}]
        with self.assertRaisesRegex(ValueError, 'S09-1-intro: not in the queue'):
            build_fixture(votes=votes, queue=make_queue(), judge_key=make_judge_key(), checkpoint_runs=make_runs())


class TestCareerContext(TestCase):
    """
    Scenario: Each career's description and family come from its first queue item.
    """

    def test_the_first_item_per_career_supplies_the_context(self):
        queue = make_queue()
        queue['ladders'][1]['family_description'] = 'a later, different description'
        context = career_context_from_queue(queue)
        self.assertEqual(list(context), [ALPHA, BETA])
        self.assertEqual(context[ALPHA], {
            'career_description': 'Alpha Analysts do alpha work.',
            'family_titles': [ALPHA, 'Junior Alpha Analyst'], 'family_size': 2,
        })

    def test_the_family_size_falls_back_to_the_titles_listed(self):
        queue = make_queue()
        del queue['ladders'][0]['careers_covered']
        self.assertEqual(career_context_from_queue(queue)[ALPHA]['family_size'], 2)


class TestReplayCareer(TestCase):
    """
    Scenario: A replay runs the app's selection and judge on the stored window, and exports a
    run ``select_shapes`` reads like any collection run.
    """

    def variants(self):
        return [
            Variant(strategy='shape_cut', requested_size=2, shape=(2, 0, 0),
                    courses=[candidate('AlphaX+I1'), candidate('RuriX+I2')]),
            Variant(strategy='shape_pick', requested_size=2, shape=(0, 2, 0), courses=[candidate('AlphaX+M1')]),
            Variant(strategy='model_sized', requested_size=None,
                    courses=[candidate('AlphaX+I1'), candidate('RuriX+I2')], trace=dict(TRACE)),
            Variant(strategy='shape_pick_v2', requested_size=3, shape=(1, 1, 1), trace=dict(TRACE),
                    courses=[candidate('AlphaX+I1'), candidate('AlphaX+M1'), candidate('AlphaX+A1')],
                    seats=[{'key': 'AlphaX+I1', 'level': 'Introductory', 'rule': 'flagship', 'reason': ''}]),
        ]

    def replay(self, **kwargs):
        """Replay Alpha Analyst with ``build_variants`` and ``judge_pathway`` patched."""
        run = make_runs()[0]
        with mock.patch.object(pathway_variants, 'build_variants', return_value=self.variants()) as build, \
                mock.patch.object(review_feedback.judging, 'judge_pathway', side_effect=fake_judgement) as judge:
            result = replay_career(
                run, strategies=['shape_cut', 'shape_pick', 'model_sized', 'shape_pick_v2'],
                shapes=['2/0/0', '0/2/0', '1/1/1'], career_skills=['alpha', 'analysis'],
                career_description='Analyses alpha.', family_titles=('Junior Alpha Analyst',), family_size=2,
                **kwargs,
            )
        return result, build, judge

    def test_selection_runs_on_the_stored_window_with_no_sizes(self):
        policy, backend = object(), object()
        _, build, _ = self.replay(policy=policy, variant_backend=backend)
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs['ordered_candidates'], WINDOW)
        self.assertEqual(kwargs['sizes'], [])
        self.assertEqual(kwargs['shapes'], ['2/0/0', '0/2/0', '1/1/1'])
        self.assertEqual(kwargs['trace_prefix'], f'replay:{ALPHA}')
        self.assertIs(kwargs['policy'], policy)
        self.assertIs(kwargs['backend'], backend)
        self.assertEqual((kwargs['career_description'], kwargs['family_titles'], kwargs['family_size']),
                         ('Analyses alpha.', ['Junior Alpha Analyst'], 2))

    def test_each_distinct_course_list_is_judged_once_per_rubric_with_full_candidates(self):
        result, _, judge = self.replay(rubrics=('v1', 'v2'))
        self.assertEqual(judge.call_count, 4)
        self.assertEqual(sorted({call.kwargs['rubric'] for call in judge.call_args_list}), ['v1', 'v2'])
        first = judge.call_args_list[0].kwargs
        self.assertEqual(first['courses'], [BY_KEY['AlphaX+I1'], BY_KEY['RuriX+I2']])
        self.assertEqual(first['career_description'], 'Analyses alpha.')
        by_label = {variant['label']: variant for variant in result['variants']}
        self.assertEqual(by_label['model_sized:2-5']['judgement']['same_as'], 'shape_cut:2/0/0')
        self.assertEqual(by_label['model_sized:2-5']['judgement_v2']['same_as'], 'shape_cut:2/0/0')
        self.assertIsNone(by_label['shape_pick:0/2/0']['judgement'])
        self.assertEqual(result['replay']['model_calls'], {'variant_arms': 2, 'judge': 4, 'total': 6})

    def test_the_run_has_the_collection_shape_and_variants_their_own_properties(self):
        result, _, _ = self.replay()
        self.assertLessEqual(set(CareerRun(requested_name='x').to_dict()), set(result))
        self.assertEqual((result['workflow_uuid'], result['pathway'], result['judgement']),
                         (f'replay:{ALPHA}', None, None))
        self.assertEqual(result['candidates'], WINDOW)
        by_label = {variant['label']: variant for variant in result['variants']}
        intro = by_label['shape_cut:2/0/0']
        self.assertEqual((intro['shape'], intro['complete'], intro['violations']), ('2/0/0', True, []))
        self.assertEqual(intro['level_mix'], {'Introductory': 2, 'Intermediate': 0, 'Advanced': 0})
        self.assertEqual(intro['seats'], [])
        self.assertFalse(by_label['shape_pick:0/2/0']['complete'])
        self.assertEqual(by_label['shape_pick_v2:1/1/1']['seats'][0]['rule'], 'flagship')
        self.assertIsNone(by_label['shape_cut:2/0/0']['judgement_v2'])
        json.dumps(result)

    def test_select_shapes_reads_the_replay(self):
        result, _, _ = self.replay(rubrics=('v1', 'v2'))
        for rubric in ('v1', 'v2'):
            tiers = {tier['tier']: tier['pick'] for tier in shape_review.select_shapes(result, rubric=rubric)['tiers']}
            self.assertEqual(tiers['intro']['labels'], ['shape_cut:2/0/0', 'model_sized:2-5'])
            self.assertEqual(tiers['ladder']['labels'], ['shape_pick_v2:1/1/1'])
            self.assertIsNone(tiers['intermediate'])

    def test_an_app_that_cannot_take_a_set_argument_refuses_rather_than_dropping_it(self):
        # pylint: disable=unused-argument
        def older_build_variants(*, career_name, career_skills, ordered_candidates, sizes, strategies,
                                 trace_prefix, backend=None, shapes=()):
            """``build_variants`` as it was before it took a policy or career context."""
            return []
        run = make_runs()[0]
        with mock.patch.object(pathway_variants, 'build_variants', older_build_variants):
            result = replay_career(run, strategies=['shape_cut'], shapes=['2/0/0'], career_skills=['alpha'])
            self.assertEqual(result['variants'], [])
            for kwargs in ({'policy': object()}, {'career_description': 'x'}):
                with self.assertRaises(ReplayContractError):
                    replay_career(run, strategies=['shape_cut'], shapes=['2/0/0'], career_skills=['alpha'], **kwargs)

    def test_the_call_bound_reuses_the_apps_estimate_less_the_delivered_pathway(self):
        bound = replay_call_bound(strategies=['shape_cut', 'shape_pick', 'model_sized'],
                                  shapes=['2/0/0', '0/2/0', '2/2/1'], rubrics=['v1', 'v2'])
        # Arms: 3 shape picks + 1 model-sized. Judged: 7 variants x 2 rubrics.
        self.assertEqual(bound, 4 + 14)
        self.assertEqual(replay_call_bound(strategies=['shape_cut'], shapes=['2/0/0'], rubrics=['v1']), 1)


class EchoJudgeBackend:
    """A judge backend that rates every course it is shown on topic, under either rubric."""

    def __init__(self):
        self.calls = []

    def complete(self, **kwargs):
        """Stand in for ``ModelBackend.complete``."""
        self.calls.append(kwargs)
        keys = re.findall(r'\[([^\]\s]+\+[^\]\s]+)\]', kwargs['user_content'])
        courses = [{'key': key, 'on_topic': True, 'too_specific': False, 'redundant_with': '',
                    'level_mismatch': False, 'role_misfit': False} for key in keys]
        return ModelResponse(content=json.dumps({'verdict': 'good', 'reason': 'fits', 'courses': courses}),
                             backend='fake', model='fake-judge')


class PickBackend:
    """A selection backend that always asks for the same keys; the app enforces each shape."""

    def __init__(self, keys):
        self.keys, self.calls = keys, []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return ModelResponse(content=json.dumps({'keys': self.keys}), backend='fake', model='fake-pick')


@unittest.skipUnless(
    'policy' in inspect.signature(pathway_variants.build_variants).parameters,
    'build_variants does not take an editorial policy yet',
)
class TestReplayAgainstTheApp(TestCase):
    """
    Scenario: Unpatched, a replay drives the app's real arms and judge through fake backends.
    """

    def test_a_replay_through_the_real_arms_and_judge_is_picked_from(self):
        picker = PickBackend(['AlphaX+I1', 'RuriX+I2', 'AlphaX+M1', 'AlphaX+A1'])
        judge = EchoJudgeBackend()
        result = replay_career(
            make_runs()[0], strategies=['shape_cut', 'shape_pick', 'shape_pick_v2'], shapes=['2/0/0', '1/1/1'],
            career_skills=['alpha'], career_description='Analyses alpha.', family_titles=['Junior Alpha Analyst'],
            family_size=2, policy=None, rubrics=('v1', 'v2'), variant_backend=picker, judge_backend=judge,
        )
        self.assertEqual(len(picker.calls), 4)
        self.assertTrue(any('Analyses alpha.' in call['user_content'] for call in picker.calls))
        # Two distinct course lists (every arm agrees on each shape), under two rubrics.
        self.assertEqual(len(judge.calls), 4)
        self.assertTrue(any('Junior Alpha Analyst' in call['user_content'] for call in judge.calls))
        self.assertEqual(result['replay']['model_calls'], {'variant_arms': 4, 'judge': 4, 'total': 8})
        for rubric in ('v1', 'v2'):
            tiers = {tier['tier']: tier['pick'] for tier in shape_review.select_shapes(result, rubric=rubric)['tiers']}
            self.assertEqual({course['key'] for course in tiers['intro']['courses']}, {'AlphaX+I1', 'RuriX+I2'})
            self.assertEqual({course['key'] for course in tiers['ladder']['courses']},
                             {'AlphaX+I1', 'AlphaX+M1', 'AlphaX+A1'})

    def test_an_editorial_snapshot_policy_reaches_the_real_arms(self):
        from enterprise_access.apps.pathway_editorial.api import (  # pylint: disable=import-outside-toplevel
            EditorialPolicy
        )
        policy = EditorialPolicy.from_dict({'excluded_keys': ['RuriX+I2']})
        result = replay_career(
            make_runs()[0], strategies=['shape_cut'], shapes=['2/0/0'], career_skills=['alpha'], policy=policy,
            judge_backend=EchoJudgeBackend(),
        )
        tiers = {tier['tier']: tier['pick'] for tier in shape_review.select_shapes(result)['tiers']}
        self.assertEqual([course['key'] for course in tiers['intro']['courses']], ['AlphaX+I1', 'GenX+I3'])
        self.assertTrue(result['replay']['policy'])


class TestReplayCheckpoint(TestCase):
    """
    Scenario: Replays are appended as they finish and read back one per career.
    """

    def test_later_lines_win_errors_are_dropped_and_a_torn_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'replay.jsonl'
            append_replay(path, replay_run(ALPHA, []))
            append_replay(path, {**replay_run(BETA, []), 'error': 'boom'})
            later = pathway_dict('shape_cut:2/0/0', ['AlphaX+I1', 'RuriX+I2'])
            append_replay(path, {**replay_run(ALPHA, []), 'variants': [later]})
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write('{"requested_name": "Tor')
            runs = load_replays(path)
        self.assertEqual([run['requested_name'] for run in runs], [ALPHA])
        self.assertEqual(len(runs[0]['variants']), 1)
        self.assertEqual(load_replays(Path(tmp) / 'missing.jsonl'), [])

    def test_career_inputs_come_from_the_replay_record(self):
        inputs = career_inputs_from_replays([replay_run(ALPHA, [])])
        self.assertEqual(inputs[ALPHA]['career_skills'], ['alpha', 'analysis'])
        self.assertEqual(inputs[ALPHA]['candidates'], WINDOW)
        self.assertEqual(inputs[ALPHA]['family_size'], 2)


class TestScoreSwaps(TestCase):
    """
    Scenario: A replay's picks are scored against the reviewer's swaps, kept courses and "nothing" slots.
    """

    def replays(self):
        return [replay_run(ALPHA, [
            pathway_dict('shape_cut:2/0/0', ['AlphaX+I1', 'GenX+I3']),
            pathway_dict('shape_cut:0/2/0', ['AlphaX+M1', 'AlphaX+M2']),
            pathway_dict('shape_cut:1/1/1', ['GenX+I3', 'AlphaX+M1', 'AlphaX+A2']),
        ])]

    def test_hits_and_retention_per_slot_overall_and_by_theme(self):
        scores = score_swaps(make_fixture(), self.replays(), rubric='v1')
        slots = {(row['item_id'], row['step']): row for row in scores['slots']}
        self.assertEqual(
            {key: (row['hit_best'], row['hit_any'], row['dropped_retained']) for key, row in slots.items()},
            {('S01-1-intro', 2): (True, True, False), ('S01-3-ladder', 1): (False, True, False)},
        )
        overall = scores['overall']
        self.assertEqual((overall['slots'], overall['with_pick'], overall['hit_best'], overall['hit_any']),
                         (2, 2, 1, 2))
        self.assertEqual((overall['hit_best_rate'], overall['dropped_retained_rate']), (0.5, 0.0))
        self.assertEqual(scores['by_theme']['regional']['hit_best'], 1)
        self.assertEqual(scores['by_theme']['redundant']['hit_any'], 1)
        self.assertEqual((scores['kept_good']['retained'], scores['kept_good']['retention_rate']), (2, 1.0))
        self.assertEqual(scores['nothing_would_work']['dropped_retained'], 0)
        # The ladder pick still holds the course dropped without a pick; the skipped career's drop is not scored.
        self.assertEqual((scores['dropped_without_pick']['slots'], scores['dropped_without_pick']['dropped_retained']),
                         (1, 1))
        self.assertEqual(scores['careers_missing'], [])

    def test_a_career_not_replayed_is_counted_apart_not_as_a_miss(self):
        scores = score_swaps(make_fixture(), [], rubric='v1')
        self.assertEqual((scores['overall']['not_replayed'], scores['overall']['hit_best_rate']), (2, None))
        self.assertEqual(scores['careers_missing'], [ALPHA])

    def test_the_v2_picks_are_read_from_the_v2_judgements(self):
        replays = [replay_run(ALPHA, [
            pathway_dict('shape_cut:2/0/0', ['AlphaX+I1', 'RuriX+I2']),
            pathway_dict('shape_pick_v2:2/0/0', ['AlphaX+I1', 'GenX+I3'], rubric_field='judgement_v2'),
        ])]
        self.assertFalse(score_swaps(make_fixture(), replays, rubric='v1')['overall']['hit_best'])
        self.assertEqual(score_swaps(make_fixture(), replays, rubric='v2')['overall']['hit_best'], 1)

    def test_a_selector_without_rubrics_refuses_v2(self):
        def older_select_shapes(run):  # pylint: disable=unused-argument
            return {'tiers': []}
        with mock.patch.object(shape_review, 'select_shapes', older_select_shapes):
            score_swaps(make_fixture(), self.replays(), rubric='v1')
            with self.assertRaises(ReplayContractError):
                score_swaps(make_fixture(), self.replays(), rubric='v2')


class TestJudgePrefersReplacement(TestCase):
    """
    Scenario: The judge sees each item's pathway as reviewed and with one swap made.
    """

    CAREERS = {ALPHA: {'career_skills': ['alpha'], 'candidates': WINDOW, 'career_description': 'desc',
                       'family_titles': ['Junior Alpha Analyst'], 'family_size': 2}}

    @staticmethod
    def prefers_swaps(**kwargs):
        """A judge that rates a pathway good only when it holds one of the reviewer's replacements."""
        keys = [course['key'] for course in kwargs['courses']]
        result = fake_judgement(**kwargs)
        if not {'GenX+I3', 'CommX+I4'} & set(keys):
            result['verdict'] = 'weak'
        return result

    def test_the_judge_agreeing_with_every_swap_and_each_pathway_judged_once(self):
        fixture = make_fixture()
        with mock.patch.object(review_feedback.judging, 'judge_pathway', side_effect=self.prefers_swaps) as judge:
            result = judge_prefers_replacement(fixture, careers=self.CAREERS, rubric='v2', max_calls=10)
        self.assertEqual(judge_swap_call_bound(fixture), 5)
        self.assertEqual((result['calls_issued'], judge.call_count), (5, 5))
        self.assertEqual([row['prefers'] for row in result['pairs']], ['replacement'] * 3)
        self.assertEqual(result['overall']['agreement_rate'], 1.0)
        self.assertEqual(result['by_theme']['redundant']['replacement'], 2)
        call = judge.call_args_list[0].kwargs
        self.assertEqual((call['rubric'], call['career_description'], call['family_size']), ('v2', 'desc', 2))
        self.assertEqual([course['key'] for course in call['courses']], ['AlphaX+I1', 'RuriX+I2'])

    def test_the_budget_is_checked_per_pair_and_never_half_judges_one(self):
        with mock.patch.object(review_feedback.judging, 'judge_pathway', side_effect=self.prefers_swaps):
            result = judge_prefers_replacement(make_fixture(), careers=self.CAREERS, max_calls=3)
        self.assertEqual(result['calls_issued'], 2)
        self.assertTrue(result['budget_exhausted'])
        self.assertEqual([row['prefers'] for row in result['pairs']],
                         ['replacement', 'not_judged_budget', 'not_judged_budget'])

    def test_a_career_without_skills_is_not_judged(self):
        with mock.patch.object(review_feedback.judging, 'judge_pathway') as judge:
            result = judge_prefers_replacement(make_fixture(), careers={}, max_calls=10)
        judge.assert_not_called()
        self.assertEqual({row['prefers'] for row in result['pairs']}, {'no_career_inputs'})

    def test_the_rank_breaks_ties_on_flagged_courses(self):
        clean = {'verdict': 'good', 'n_on_topic': 2, 'n_courses': 2, 'flags': {}}
        flagged = {**clean, 'flags': {'A+1': {'too_specific': True, 'redundant_with': '', 'level_mismatch': False,
                                              'role_misfit': False}}}
        self.assertGreater(judgement_rank(clean), judgement_rank(flagged))
        self.assertGreater(judgement_rank(flagged), judgement_rank({**clean, 'verdict': 'weak'}))
