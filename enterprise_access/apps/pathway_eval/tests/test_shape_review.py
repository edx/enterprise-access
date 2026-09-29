"""
Tests for picking the judge's best pathway per shape tier.

The properties that matter: a pathway is placed by the levels it actually landed on, never
by the shape it was asked for; only complete, gate-clean, judged pathways can be picked; and
the ranking within a tier is the documented one, deterministically.
"""
import csv
import json
import tempfile
from io import StringIO
from pathlib import Path

import ddt
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from enterprise_access.apps.pathway_eval.shape_review import (
    REVIEW_SHAPES,
    SHAPE_TIERS,
    candidates_by_tier,
    select_all,
    select_shapes,
    tier_for,
    write_selection_csv
)
from enterprise_access.apps.pathway_eval.variant_collection import CareerRun, append_checkpoint

LEVELS = ('Introductory', 'Intermediate', 'Advanced')


def mix(shape):
    return dict(zip(LEVELS, (int(part) for part in shape.split('/'))))


def pathway(label, keys, shape, *, verdict='good', n_on_topic=None, complete=True,
            violations=(), error='', judge_error=''):
    """A variant as ``PathwayAssemblyWorkflow.variants()`` returns it."""
    strategy = label.partition(':')[0]
    return {
        'label': label, 'strategy': strategy, 'shape': shape if strategy.startswith('shape_') else '',
        'courses': [{'key': key, 'title': f'Course {key}'} for key in keys],
        'complete': complete, 'level_mix': mix(shape), 'violations': list(violations), 'error': error,
        'judgement': {
            'verdict': '' if judge_error else verdict, 'reason': f'{label} reason',
            'n_on_topic': len(keys) if n_on_topic is None else n_on_topic,
            'on_topic': {key: True for key in keys}, 'error': judge_error,
        },
    }


def run(variants, *, default=None, candidates=()):
    """An exported ``CareerRun``, as ``to_dict`` writes it."""
    return {
        'requested_name': 'Data Analyst', 'career_name': 'Data Analyst', 'external_id': 'ET1',
        'workflow_uuid': 'wf', 'pathway': default, 'judgement': (default or {}).get('judgement'),
        'variants': list(variants), 'candidates': list(candidates), 'error': '', 'skipped_reason': '',
    }


@ddt.ddt
class TestTierFor(TestCase):
    """
    Scenario: A pathway's tier is read off the levels its courses landed on.
    """

    @ddt.data(
        ('2/0/0', 'intro'), ('0/2/0', 'intermediate'),
        ('2/2/1', 'ladder'), ('1/1/1', 'ladder'), ('1/2/2', 'ladder'),
        ('2/1/0', 'other'), ('0/2/1', 'other'), ('3/2/0', 'other'), ('1/0/1', 'other'),
        ('0/0/2', 'other'), ('0/0/3', 'other'),
        ('3/0/0', ''), ('0/3/0', ''), ('5/0/0', ''),
    )
    @ddt.unpack
    def test_each_mix_lands_in_its_tier(self, shape, tier):
        self.assertEqual(tier_for(mix(shape), sum(mix(shape).values())), tier)

    def test_a_course_of_unknown_level_leaves_the_pathway_unplaced(self):
        self.assertEqual(tier_for(mix('2/0/0'), 3), '')

    def test_every_review_shape_feeds_a_tier(self):
        for shape in REVIEW_SHAPES:
            self.assertTrue(tier_for(mix(shape), sum(mix(shape).values())), shape)


class TestCandidatesByTier(TestCase):
    """
    Scenario: Only pathways that met their brief compete, and they are ranked as documented.
    """

    def test_a_pathway_competes_where_its_courses_landed_not_where_it_was_aimed(self):
        # A model-sized pathway that happens to climb every rung is a ladder candidate.
        by_tier = candidates_by_tier(run([pathway('model_sized:2-5', ['A', 'B', 'C'], '1/1/1')]))

        self.assertEqual([c['labels'] for c in by_tier['ladder']], [['model_sized:2-5']])

    def test_incomplete_gated_failed_and_unjudged_pathways_do_not_compete(self):
        by_tier = candidates_by_tier(run([
            pathway('shape_cut:2/0/0', ['A', 'B'], '2/0/0', complete=False),
            pathway('shape_pick:2/0/0', ['C', 'D'], '2/0/0', violations=['over cap']),
            pathway('shape_pick:0/2/0', ['E', 'F'], '0/2/0', error='selection failed'),
            pathway('shape_cut:0/2/0', ['G', 'H'], '0/2/0', judge_error='judge request failed'),
        ]))

        self.assertEqual(sum(len(tier) for tier in by_tier.values()), 0)

    def test_verdict_outranks_precision(self):
        by_tier = candidates_by_tier(run([
            pathway('shape_cut:2/0/0', ['A', 'B'], '2/0/0', verdict='weak'),
            pathway('shape_pick:2/0/0', ['C', 'D'], '2/0/0', verdict='good', n_on_topic=1),
        ]))

        self.assertEqual(by_tier['intro'][0]['labels'], ['shape_pick:2/0/0'])

    def test_at_equal_verdict_and_precision_the_fuller_ladder_wins(self):
        by_tier = candidates_by_tier(run([
            pathway('shape_pick:1/1/1', ['A', 'B', 'C'], '1/1/1'),
            pathway('shape_cut:2/2/1', ['A', 'B', 'C', 'D', 'E'], '2/2/1'),
        ]))

        self.assertEqual(by_tier['ladder'][0]['labels'], ['shape_cut:2/2/1'])

    def test_at_a_full_tie_the_delivered_pathway_then_a_model_pick_is_preferred(self):
        default = pathway('default', ['A', 'B', 'C', 'D', 'E'], '2/2/1')
        by_tier = candidates_by_tier(run([
            pathway('shape_cut:2/2/1', ['F', 'G', 'H', 'I', 'J'], '2/2/1'),
            pathway('shape_pick:2/2/1', ['K', 'L', 'M', 'N', 'O'], '2/2/1'),
        ], default=default))

        self.assertEqual(
            [c['labels'][0] for c in by_tier['ladder']], ['default', 'shape_pick:2/2/1', 'shape_cut:2/2/1'],
        )

    def test_identical_course_lists_are_one_candidate_carrying_every_label(self):
        by_tier = candidates_by_tier(run([
            pathway('shape_cut:0/2/0', ['A', 'B'], '0/2/0'),
            pathway('shape_pick:0/2/0', ['A', 'B'], '0/2/0'),
        ]))

        self.assertEqual(by_tier['intermediate'][0]['labels'], ['shape_cut:0/2/0', 'shape_pick:0/2/0'])
        self.assertEqual(len(by_tier['intermediate']), 1)


class TestSelectShapes(TestCase):
    """
    Scenario: Each career gets one entry per tier, a pick or a reason there is none.
    """

    def test_every_tier_is_reported_in_order_with_its_pick_or_why_not(self):
        candidates = [{'key': 'A', 'level_type': 'Introductory'}, {'key': 'B', 'level_type': 'Introductory'},
                      {'key': 'C', 'level_type': 'Advanced'}]
        selection = select_shapes(run(
            [pathway('shape_cut:2/0/0', ['A', 'B'], '2/0/0'),
             pathway('shape_pick:2/0/0', ['B', 'A2'], '2/0/0', verdict='weak')],
            candidates=candidates,
        ))

        tiers = {tier['tier']: tier for tier in selection['tiers']}
        self.assertEqual([tier['tier'] for tier in selection['tiers']], [tier.key for tier in SHAPE_TIERS])
        self.assertEqual(tiers['intro']['pick']['labels'], ['shape_cut:2/0/0'])
        self.assertEqual(tiers['intro']['n_candidates'], 2)
        self.assertEqual(tiers['intro']['runners_up'][0]['verdict'], 'weak')
        self.assertIsNone(tiers['intermediate']['pick'])
        self.assertIn('0 Intermediate', tiers['intermediate']['why_none'])
        self.assertEqual(selection['supply'], {'Introductory': 2, 'Intermediate': 0, 'Advanced': 1})

    def test_runs_that_never_reached_the_workflow_are_left_out(self):
        skipped = dict(run([]), workflow_uuid='', skipped_reason='no career')

        self.assertEqual(select_all([skipped, run([])])[0]['career'], 'Data Analyst')
        self.assertEqual(len(select_all([skipped])), 0)

    def test_the_csv_has_one_row_per_career_and_tier(self):
        selections = select_all([run([pathway('shape_cut:2/0/0', ['A', 'B'], '2/0/0')])])
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'picks.csv'
            count = write_selection_csv(selections, path)
            rows = list(csv.DictReader(path.open(encoding='utf-8')))

        self.assertEqual(count, len(SHAPE_TIERS))
        self.assertEqual((rows[0]['status'], rows[0]['course_keys']), ('picked', 'A | B'))
        self.assertEqual(rows[1]['status'], 'none')


class TestSelectPathwayShapesCommand(TestCase):
    """
    Scenario: Picks are made offline from a collection's export or checkpoint.
    """

    def test_it_reads_an_export_and_writes_the_picks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / 'variants.json'
            source.write_text(json.dumps({'runs': [run([pathway('shape_cut:0/2/0', ['A', 'B'], '0/2/0')])]}))
            target = Path(tmpdir) / 'picks.json'
            out = StringIO()

            call_command('select_pathway_shapes', input_json=str(source), output_json=str(target), stdout=out)

            picks = json.loads(target.read_text())['selections'][0]
        self.assertEqual(picks['tiers'][1]['pick']['labels'], ['shape_cut:0/2/0'])
        self.assertIn('good 0/2/0', out.getvalue())

    def test_it_reads_a_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            checkpoint = Path(tmpdir) / 'runs.jsonl'
            default = pathway('default', list('ABCDE'), '2/2/1')
            append_checkpoint(checkpoint, CareerRun.from_dict(
                run([pathway('shape_cut:2/0/0', ['A', 'B'], '2/0/0')], default=default),
            ))
            out = StringIO()

            call_command('select_pathway_shapes', checkpoint=str(checkpoint), stdout=out)

        self.assertIn('good 2/0/0', out.getvalue())
        self.assertIn('good 2/2/1', out.getvalue())

    def test_an_empty_collection_is_a_command_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / 'variants.json'
            source.write_text(json.dumps({'runs': []}))
            with self.assertRaisesRegex(CommandError, 'no runs'):
                call_command('select_pathway_shapes', input_json=str(source), stdout=StringIO())


def with_v2(pathway_dict, *, verdict='good', flagged=(), n_on_topic=None):
    """``pathway_dict`` carrying a v2 judgement, flagging the given courses."""
    keys = [course['key'] for course in pathway_dict['courses']]
    return {**pathway_dict, 'judgement_v2': {
        'verdict': verdict, 'reason': 'v2 reason', 'rubric': 'v2',
        'n_on_topic': len(keys) if n_on_topic is None else n_on_topic,
        'on_topic': {key: True for key in keys}, 'error': '',
        'flags': {key: {'too_specific': key in flagged, 'redundant_with': '', 'level_mismatch': False,
                        'role_misfit': False} for key in keys},
    }}


class TestSelectShapesUnderV2(TestCase):
    """
    Scenario: Picks can be ranked by the v2 rubric, which prefers fewer flagged courses at a tie.
    """

    def test_fewer_flagged_courses_win_once_verdict_and_on_topic_tie(self):
        flagged = with_v2(pathway('shape_pick:2/0/0', ['A', 'B'], '2/0/0'), flagged=('A',))
        clean = with_v2(pathway('shape_cut:2/0/0', ['C', 'D'], '2/0/0'))

        v1 = candidates_by_tier(run([flagged, clean]))
        v2 = candidates_by_tier(run([flagged, clean]), rubric='v2')

        # v1 flags nothing, so its usual tie-break (a model pick over a cut) decides.
        self.assertEqual(v1['intro'][0]['labels'], ['shape_pick:2/0/0'])
        self.assertEqual(v2['intro'][0]['labels'], ['shape_cut:2/0/0'])
        self.assertEqual([c['n_flagged'] for c in v2['intro']], [0, 1])

    def test_the_v2_verdict_still_outranks_flags(self):
        flagged_good = with_v2(pathway('shape_pick:2/0/0', ['A', 'B'], '2/0/0'), flagged=('A', 'B'))
        clean_weak = with_v2(pathway('shape_cut:2/0/0', ['C', 'D'], '2/0/0'), verdict='weak')

        self.assertEqual(candidates_by_tier(run([flagged_good, clean_weak]), rubric='v2')['intro'][0]['labels'],
                         ['shape_pick:2/0/0'])

    def test_a_pathway_without_a_v2_judgement_cannot_be_picked_under_v2(self):
        judged_v1_only = pathway('shape_cut:0/2/0', ['A', 'B'], '0/2/0')

        self.assertEqual(candidates_by_tier(run([judged_v1_only]), rubric='v2')['intermediate'], [])

    def test_the_delivered_pathway_competes_on_its_v2_judgement(self):
        default = pathway('default', list('ABCDE'), '2/2/1')
        exported = dict(run([], default=default), judgement_v2=with_v2(default)['judgement_v2'])

        ladder = candidates_by_tier(exported, rubric='v2')['ladder'][0]

        self.assertEqual((ladder['labels'], ladder['reason'], ladder['rubric']), (['default'], 'v2 reason', 'v2'))

    def test_picks_record_the_rubric_that_chose_them(self):
        exported = run([with_v2(pathway('shape_cut:2/0/0', ['A', 'B'], '2/0/0'), flagged=('B',))])

        selection = select_all([exported], rubric='v2')[0]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'picks.csv'
            write_selection_csv([selection], path)
            first = next(csv.DictReader(path.open(encoding='utf-8')))

        self.assertEqual(selection['rubric'], 'v2')
        self.assertEqual(selection['tiers'][0]['pick']['rubric'], 'v2')
        self.assertEqual((first['rubric'], first['n_flagged']), ('v2', '1'))
        self.assertEqual(select_all([exported])[0]['rubric'], 'v1')

    def test_an_unknown_rubric_is_refused(self):
        with self.assertRaises(ValueError):
            select_all([run([])], rubric='v3')

    def test_the_command_ranks_by_the_rubric_it_is_given(self):
        exported = run([
            with_v2(pathway('shape_pick:2/0/0', ['A', 'B'], '2/0/0'), flagged=('A',)),
            with_v2(pathway('shape_cut:2/0/0', ['C', 'D'], '2/0/0')),
        ])
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / 'variants.json'
            source.write_text(json.dumps({'runs': [exported]}))
            target = Path(tmpdir) / 'picks.json'
            out = StringIO()

            call_command('select_pathway_shapes', input_json=str(source), output_json=str(target),
                         rubric='v2', stdout=out)
            picks = json.loads(target.read_text())

        self.assertEqual(picks['rubric'], 'v2')
        self.assertEqual(picks['selections'][0]['tiers'][0]['pick']['labels'], ['shape_cut:2/0/0'])
        self.assertIn('ranked by judge rubric v2', out.getvalue())
