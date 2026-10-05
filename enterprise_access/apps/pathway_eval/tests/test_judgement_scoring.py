"""
Tests for scoring pathways against the reviewer's course judgements.

The properties that matter: each kind of vote puts a course in the set the module docstring says,
and the two legacy drop markers endorse nothing; a course in both sets is contested and scored as
neither; the measures are the reference script's, with unknown courses left out of the endorsed
share; the builder reads only the named reviewer and writes no identity; and the committed fixture is
exactly what the builder makes of the committed votes export. Nothing here reads the live bench.
"""
import json
import sqlite3
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from enterprise_access.apps.pathway_eval import judgement_scoring
from enterprise_access.apps.pathway_eval.judgement_scoring import (
    DEFAULT_FIXTURE,
    ReviewJudgements,
    build_fixture,
    item_careers,
    load_runs,
    pathways_from_runs,
    score_groups,
    score_pathways,
    votes_export_bytes
)

INTRO, INTER, ADV = 'Introductory', 'Intermediate', 'Advanced'
ALPHA, BETA, GAMMA = 'Alpha Analyst', 'Beta Builder', 'Gamma Guide'


def vote(item_id, verdict, courses, drops=(), replacements=None, suggestions=None):
    """A vote as ``load_bench_votes`` returns it; ``courses`` are ``(step, level, key)``."""
    return {
        'item_id': item_id,
        'verdict': verdict,
        'dropped_steps': list(drops),
        'replacements': replacements or {},
        'suggestions': suggestions or {},
        'courses': [{'step': step, 'level': level, 'key': key} for step, level, key in courses],
    }


def fixture_for(votes, careers=(ALPHA, BETA), blind_key=None, **kwargs):
    return build_fixture(votes=votes, careers_in_order=list(careers), blind_key_items=blind_key or {}, **kwargs)


def sets_of(fixture, career, level):
    slot = fixture['careers'][career]['levels'].get(level, {})
    names = ('endorsed', 'rejected', 'contested')
    return {name: sorted(entry['key'] for entry in slot.get(name, [])) for name in names}


def as_sets(found):
    return {slot: frozenset(keys) for slot, keys in (found or {}).items()}


def judgements(endorsed=None, rejected=None, contested=None, splits=None):
    return ReviewJudgements(endorsed=as_sets(endorsed), rejected=as_sets(rejected), contested=as_sets(contested),
                            splits=splits or {ALPHA: 'calibration', BETA: 'held_out'})


class BuildJudgementsTests(SimpleTestCase):
    """What each kind of vote puts in each set."""

    def test_every_kind_of_endorsement(self):
        votes = [
            # Kept step 1 with suggestions; dropped step 2 for a best pick and an also-fine pick.
            vote('S01-1-intro', 'needs_work', [(1, INTRO, 'k1'), (2, INTRO, 'k2')], drops=[2],
                 replacements={'2': {'best': 'best', 'also': ['also']}}, suggestions={'1': ['suggested']}),
            # The legacy bench's bare-key pick.
            vote('S01-2-inter', 'needs_work', [(1, INTER, 'k3')], drops=[1], replacements={'1': 'legacy'}),
            # Rated good: every kept course is endorsed.
            vote('S01-3-ladder', 'good', [(1, INTRO, 'k4'), (2, ADV, 'k5')]),
        ]
        fixture = fixture_for(votes)
        self.assertEqual(sets_of(fixture, ALPHA, INTRO),
                         {'endorsed': ['also', 'best', 'k4', 'suggested'], 'rejected': ['k2'], 'contested': []})
        self.assertEqual(sets_of(fixture, ALPHA, INTER), {'endorsed': ['legacy'], 'rejected': ['k3'], 'contested': []})
        self.assertEqual(sets_of(fixture, ALPHA, ADV), {'endorsed': ['k5'], 'rejected': [], 'contested': []})

    def test_a_drop_without_a_pick_endorses_nothing(self):
        votes = [vote('S01-1-intro', 'needs_work', [(1, INTRO, 'k1'), (2, INTRO, 'k2')], drops=[1, 2],
                      replacements={'1': '', '2': '__none__'})]
        self.assertEqual(sets_of(fixture_for(votes), ALPHA, INTRO),
                         {'endorsed': [], 'rejected': ['k1', 'k2'], 'contested': []})

    def test_kept_courses_of_a_pathway_not_rated_good_are_not_endorsed(self):
        votes = [vote('S01-1-intro', 'needs_work', [(1, INTRO, 'k1'), (2, INTRO, 'k2')], drops=[2])]
        self.assertEqual(sets_of(fixture_for(votes), ALPHA, INTRO),
                         {'endorsed': [], 'rejected': ['k2'], 'contested': []})

    def test_a_course_in_both_sets_is_contested_and_in_neither(self):
        votes = [
            vote('S01-1-intro', 'good', [(1, INTRO, 'both'), (2, INTRO, 'kept')]),
            vote('S01-4-other', 'needs_work', [(1, INTRO, 'both'), (2, INTER, 'both')], drops=[1, 2]),
        ]
        fixture = fixture_for(votes)
        self.assertEqual(sets_of(fixture, ALPHA, INTRO), {'endorsed': ['kept'], 'rejected': [], 'contested': ['both']})
        # The same course at another level is another slot, and not contested there.
        self.assertEqual(sets_of(fixture, ALPHA, INTER), {'endorsed': [], 'rejected': ['both'], 'contested': []})
        entry = fixture['careers'][ALPHA]['levels'][INTRO]['contested'][0]
        self.assertEqual(entry, {'key': 'both', 'rounds': [1], 'endorsed_in': ['S01-1-intro'],
                                 'rejected_in': ['S01-4-other']})
        self.assertEqual(fixture['built_from']['counts']['contested'], 1)

    def test_provenance_records_rounds_items_and_how(self):
        votes = [
            vote('S02-1-intro', 'good', [(1, INTRO, 'k1')]),
            vote('R2-02-1a', 'needs_work', [(1, INTRO, 'k0')], drops=[1], replacements={'1': {'best': 'k1'}}),
        ]
        fixture = fixture_for(votes, blind_key={'R2-02-1a': {'career': BETA}})
        entry = fixture['careers'][BETA]['levels'][INTRO]['endorsed'][0]
        self.assertEqual(entry, {'key': 'k1', 'rounds': [1, 2], 'items': ['R2-02-1a', 'S02-1-intro'],
                                 'via': ['best_pick', 'kept_in_good']})

    def test_items_map_to_careers_by_rank_and_by_blind_key(self):
        careers = item_careers([ALPHA, BETA], {'R2-01-1a': {'career': BETA}})
        self.assertEqual(careers['S01-3-ladder'], ALPHA)
        self.assertEqual(careers['S02-2-inter'], BETA)
        self.assertEqual(careers['R2-01-1a'], BETA)
        self.assertNotIn('S03-1-intro', careers)

    def test_a_vote_on_an_unknown_item_is_skipped_but_counted(self):
        fixture = fixture_for([vote('S09-1-intro', 'good', [(1, INTRO, 'k1')]),
                               vote('S01-1-intro', 'good', [(1, INTRO, 'k2')])])
        counts = fixture['built_from']['counts']
        self.assertEqual((counts['votes'], counts['votes_with_a_career'], counts['endorsed']), (2, 1, 1))

    def test_a_suggestion_on_a_step_the_item_lacks_is_unplaced(self):
        fixture = fixture_for([vote('S01-1-intro', 'needs_work', [(1, INTRO, 'k1')], suggestions={'7': ['k9']})])
        self.assertEqual(fixture['built_from']['counts']['unplaced_judgements'], 1)
        self.assertEqual(fixture['careers'][ALPHA]['levels'], {})

    def test_split_by_rank(self):
        careers = [f'Career {n}' for n in range(1, 13)]
        fixture = fixture_for([], careers=careers)
        splits = [fixture['careers'][career]['split'] for career in careers]
        self.assertEqual(splits, ['calibration'] * 9 + ['held_out'] * 3)
        fixture = fixture_for([], careers=careers, calibration_careers=2)
        self.assertEqual(fixture['careers']['Career 3']['split'], 'held_out')
        self.assertEqual(fixture['built_from']['counts']['calibration']['careers'], 2)

    def test_round_trip_through_review_judgements(self):
        votes = [vote('S01-1-intro', 'needs_work', [(1, INTRO, 'k1'), (2, INTRO, 'k2')], drops=[2])]
        loaded = ReviewJudgements.from_fixture(fixture_for(votes, calibration_careers=1))
        self.assertEqual(loaded.standing(ALPHA, INTRO, 'k2'), 'rejected')
        self.assertEqual(loaded.standing(ALPHA, INTRO, 'k1'), 'unknown')
        self.assertEqual(loaded.split_of(BETA), 'held_out')
        self.assertEqual(loaded.split_of(GAMMA), '')
        with self.assertRaises(ValueError):
            ReviewJudgements.from_fixture({'schema': 99, 'careers': {}})


class ScorePathwaysTests(SimpleTestCase):
    """The measures, which must be the reference script's."""

    def setUp(self):
        self.judgements = judgements(
            endorsed={(ALPHA, INTRO): {'good1', 'good2'}, (ALPHA, INTER): {'good3'}},
            rejected={(ALPHA, INTRO): {'bad1'}},
            contested={(ALPHA, INTRO): {'torn'}},
        )

    def test_the_three_measures(self):
        pathways = [
            [(ALPHA, INTRO, 'good1'), (ALPHA, INTER, 'good3')],            # clean
            [(ALPHA, INTRO, 'bad1'), (ALPHA, INTRO, 'good2')],             # holds a rejected course
            [(ALPHA, INTRO, 'never-seen'), (ALPHA, INTER, 'also-unseen')],  # nothing known
            [],                                                             # not counted
        ]
        result = score_pathways(pathways, self.judgements)
        self.assertEqual(result['pathways'], 3)
        self.assertEqual(result['holding_a_rejected_course'], 1)
        self.assertEqual(result['rejected_rate'], 0.3333)
        self.assertEqual(result['clean_pathways'], 1)
        self.assertEqual(result['clean_rate'], 0.3333)
        self.assertEqual(result['courses_with_a_known_standing'], 4)
        self.assertEqual(result['endorsed_share'], 0.75)
        self.assertEqual((result['courses'], result['unknown_courses']), (6, 2))

    def test_unknown_courses_are_left_out_of_the_endorsed_share(self):
        all_unknown = score_pathways([[(ALPHA, INTRO, 'x'), (ALPHA, INTRO, 'y')]], self.judgements)
        self.assertEqual(all_unknown['endorsed_share'], None)
        self.assertEqual(all_unknown['rejected_rate'], 0.0)
        self.assertEqual(all_unknown['clean_rate'], 0.0)
        mixed = score_pathways([[(ALPHA, INTRO, 'good1'), (ALPHA, INTRO, 'x'), (ALPHA, INTRO, 'y')]], self.judgements)
        self.assertEqual(mixed['endorsed_share'], 1.0)

    def test_a_contested_course_is_scored_as_neither(self):
        result = score_pathways([[(ALPHA, INTRO, 'torn'), (ALPHA, INTRO, 'good1')]], self.judgements)
        self.assertEqual(result['holding_a_rejected_course'], 0)
        self.assertEqual(result['courses_with_a_known_standing'], 1)
        self.assertEqual(result['contested_courses'], 1)
        alone = score_pathways([[(ALPHA, INTRO, 'torn')]], self.judgements)
        self.assertEqual((alone['clean_pathways'], alone['endorsed_share']), (0, None))

    def test_standing_is_per_career_and_level(self):
        result = score_pathways([[(ALPHA, ADV, 'bad1'), (BETA, INTRO, 'good1')]], self.judgements)
        self.assertEqual((result['holding_a_rejected_course'], result['unknown_courses']), (0, 2))

    def test_no_pathways(self):
        result = score_pathways([], self.judgements)
        self.assertEqual((result['pathways'], result['rejected_rate'], result['clean_rate']), (0, None, None))


def course(key, level):
    return {'key': key, 'level_type': level, 'title': key, 'partner': ''}


def variant(label, courses, complete=True):
    return {'label': label, 'complete': complete, 'courses': courses}


def run(career, variants=(), pathway=None, requested=None):
    return {'requested_name': requested or career, 'career_name': career, 'pathway': pathway,
            'variants': list(variants), 'candidates': []}


class ReadingRunsTests(SimpleTestCase):
    """Exports and checkpoints in, pathways out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_export_a_checkpoint_and_a_replay(self):
        runs = [run(ALPHA, [variant('shape_pick_v2:2/0/0', [course('a', INTRO), course('b', INTRO)])]),
                run(BETA, pathway={'complete': True, 'courses': [course('c', INTER), course('d', INTER)]})]
        export = self.dir / 'export.json'
        export.write_text(json.dumps({'summary': [], 'runs': runs}))
        self.assertEqual([r['career_name'] for r in load_runs(export)], [ALPHA, BETA])

        # A checkpoint: later lines win, a torn line is skipped, and a replay run -- no delivered
        # pathway -- is kept, which load_checkpoint would not do.
        stale = run(ALPHA, [variant('shape_pick_v2:2/0/0', [course('z', INTRO), course('y', INTRO)])])
        lines = [json.dumps(stale), json.dumps(runs[0]), '{"torn', json.dumps(runs[1])]
        checkpoint = self.dir / 'replay.jsonl'
        checkpoint.write_text('\n'.join(lines) + '\n')
        loaded = load_runs(checkpoint)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]['variants'][0]['courses'][0]['key'], 'a')

    def test_labels_tiers_and_completeness(self):
        runs = [run(ALPHA, [
            variant('shape_pick_v2:2/0/0', [course('a', INTRO), course('b', INTRO)]),
            variant('shape_pick_v2:2/2/1', [course('a', INTRO), course('c', INTER), course('e', ADV)]),
            variant('shape_cut:0/2/0', [course('c', INTER), course('d', INTER)]),
            variant('shape_pick_v2:0/2/0', [course('c', INTER)], complete=False),
        ], pathway={'complete': True, 'courses': [course('a', INTRO), course('c', INTER)]})]

        def labels(found):
            return [p.label for p in found]

        self.assertEqual(labels(pathways_from_runs(runs)),
                         ['default', 'shape_pick_v2:2/0/0', 'shape_pick_v2:2/2/1', 'shape_cut:0/2/0'])
        self.assertEqual(labels(pathways_from_runs(runs, labels=['shape_pick_v2:*'])),
                         ['shape_pick_v2:2/0/0', 'shape_pick_v2:2/2/1'])
        self.assertEqual(labels(pathways_from_runs(runs, tiers=['intermediate', 'ladder'])),
                         ['shape_pick_v2:2/2/1', 'shape_cut:0/2/0'])
        self.assertEqual(labels(pathways_from_runs(runs, labels=['shape_pick_v2:0/2/0'], complete_only=False)),
                         ['shape_pick_v2:0/2/0'])
        found = pathways_from_runs(runs, labels=['shape_cut:*'])[0]
        self.assertEqual((found.career, found.tier, found.courses),
                         (ALPHA, 'intermediate', ((ALPHA, INTER, 'c'), (ALPHA, INTER, 'd'))))

    def test_groups_and_splits(self):
        judged = judgements(rejected={(ALPHA, INTRO): {'bad'}, (BETA, INTRO): {'bad'}})
        runs = [run(ALPHA, [variant('x', [course('bad', INTRO), course('a', INTRO)])]),
                run(BETA, [variant('x', [course('ok', INTRO), course('b', INTRO)])]),
                run(GAMMA, [variant('y', [course('bad', INTRO), course('c', INTRO)])])]
        rows = score_groups(pathways_from_runs(runs), judged, by_split=True)
        summary = [(r['group'], r['split'], r['pathways'], r['holding_a_rejected_course'], r['careers_unseen'])
                   for r in rows]
        self.assertEqual(summary, [
            ('x', 'all', 2, 1, 0), ('x', 'calibration', 1, 1, 0), ('x', 'held_out', 1, 0, 0),
            ('y', 'all', 1, 0, 1), ('y', 'unseen', 1, 0, 1),
        ])
        rows = score_groups(pathways_from_runs(runs), judged, group_by='none')
        self.assertEqual([(r['group'], r['pathways']) for r in rows], [('all', 3)])


# ---------------------------------------------------------------------------------------------
# The builder, against a synthetic bench database
# ---------------------------------------------------------------------------------------------

def make_bench_db(path, votes_by_user):
    """A bench database holding only what the builder reads: users, items and votes."""
    con = sqlite3.connect(path)
    con.executescript(
        """create table core_user (id integer primary key, username varchar(150));
           create table pathway_review_pathwayreviewitem (id integer primary key, item_id varchar(16) unique,
               payload text);
           create table pathway_review_pathwayreviewvote (id integer primary key, item_id integer,
               reviewer_id integer, verdict varchar(16), dropped_steps text, replacements text,
               suggestions text, notes text);"""
    )
    items = {}
    for user_id, (username, votes) in enumerate(votes_by_user.items(), 1):
        con.execute('insert into core_user values (?, ?)', (user_id, username))
        for one in votes:
            if one['item_id'] not in items:
                items[one['item_id']] = len(items) + 1
                payload = {'courses': [{**c, 'title': c['key']} for c in one['courses']], 'alt': {}}
                con.execute('insert into pathway_review_pathwayreviewitem values (?, ?, ?)',
                            (items[one['item_id']], one['item_id'], json.dumps(payload)))
            con.execute(
                'insert into pathway_review_pathwayreviewvote (item_id, reviewer_id, verdict, dropped_steps, '
                'replacements, suggestions, notes) values (?, ?, ?, ?, ?, ?, ?)',
                (items[one['item_id']], user_id, one['verdict'], json.dumps(one['dropped_steps']),
                 json.dumps(one['replacements']), json.dumps(one['suggestions']), 'a private note'),
            )
    con.commit()
    con.close()


def make_shape_review_dir(path, careers, blind_key_items):
    path.mkdir(parents=True, exist_ok=True)
    (path / 'careers.txt').write_text('# ranked careers\n' + '\n'.join(careers) + '\n')
    (path / 'blind_key.json').write_text(json.dumps({'items': blind_key_items, 'pairs': []}))


def _walk_keys(value):
    """Every dict key anywhere in a JSON value."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


class BuildReviewJudgementsCommandTests(SimpleTestCase):
    """The builder reads one reviewer, read-only, and writes no identity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, db, review_dir, reviewer='reviewer.one'):
        out = StringIO()
        call_command('build_review_judgements', db=str(db), shape_review_dir=str(review_dir),
                     reviewer=reviewer, output=str(self.dir / 'out' / 'fixture.json'), stdout=out)
        return json.loads((self.dir / 'out' / 'fixture.json').read_text()), out.getvalue()

    def test_a_small_synthetic_bench(self):
        mine = [
            vote('S01-1-intro', 'good', [(1, INTRO, 'k1'), (2, INTRO, 'k2')]),
            vote('R2-02-1a', 'needs_work', [(1, INTRO, 'k3'), (2, INTRO, 'k4')], drops=[2],
                 replacements={'2': {'best': 'k5', 'also': []}}),
        ]
        theirs = [vote('S01-1-intro', 'bad', [(1, INTRO, 'k1'), (2, INTRO, 'k2')], drops=[1, 2])]
        db = self.dir / 'bench.db'
        make_bench_db(db, {'reviewer.one': mine, 'someone.else': theirs})
        before = db.read_bytes()
        review_dir = self.dir / 'review'
        make_shape_review_dir(review_dir, [ALPHA, BETA], {'R2-02-1a': {'career': BETA, 'arm': 'new'}})

        fixture, output = self.build(db, review_dir)

        self.assertEqual(sets_of(fixture, ALPHA, INTRO), {'endorsed': ['k1', 'k2'], 'rejected': [], 'contested': []})
        self.assertEqual(sets_of(fixture, BETA, INTRO), {'endorsed': ['k5'], 'rejected': ['k4'], 'contested': []})
        self.assertEqual(fixture['reviewer'], 'reviewer-1')
        self.assertEqual(fixture['built_from']['database'], 'bench.db')
        self.assertEqual(set(fixture['built_from']['sources']), {'careers.txt', 'blind_key.json'})
        self.assertIn('endorsed 3  rejected 1  contested 0', output)
        self.assertEqual(db.read_bytes(), before)

        export = (self.dir / 'out' / 'fixture.votes.json').read_bytes()
        self.assertEqual(fixture['built_from']['votes_export_sha256'], judgement_scoring.sha256_bytes(export))
        written = (self.dir / 'out' / 'fixture.json').read_text() + export.decode()
        for private in ('reviewer.one', 'someone.else', 'a private note'):
            self.assertNotIn(private, written)

    def test_refuses_an_unknown_reviewer_or_a_missing_database(self):
        db = self.dir / 'bench.db'
        make_bench_db(db, {'reviewer.one': []})
        review_dir = self.dir / 'review'
        make_shape_review_dir(review_dir, [ALPHA], {})
        with self.assertRaises(CommandError):
            self.build(db, review_dir, reviewer='nobody')
        with self.assertRaises(CommandError):
            self.build(self.dir / 'missing.db', review_dir)


class CommittedFixtureTests(SimpleTestCase):
    """The committed fixture is the builder's output from the committed votes export."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixture = json.loads(DEFAULT_FIXTURE.read_text())
        cls.export_path = DEFAULT_FIXTURE.with_name(f'{DEFAULT_FIXTURE.stem}.votes.json')
        cls.export = json.loads(cls.export_path.read_text())

    def test_export_hash_and_shape(self):
        built_from = self.fixture['built_from']
        digest = judgement_scoring.sha256_bytes(self.export_path.read_bytes())
        self.assertEqual(built_from['votes_export_sha256'], digest)
        self.assertEqual(built_from['counts']['votes'], len(self.export))
        careers = self.fixture['careers']
        self.assertEqual(len(careers), 20)
        self.assertEqual(sorted(record['rank'] for record in careers.values()), list(range(1, 21)))
        self.assertTrue(all(record['split'] == ('calibration' if record['rank'] <= 9 else 'held_out')
                            for record in careers.values()))
        self.assertEqual(self.fixture['reviewer'], 'reviewer-1')
        keys = set(_walk_keys(self.fixture)) | set(_walk_keys(self.export))
        self.assertFalse(keys & {'username', 'reviewer_id', 'notes', 'reasons', 'user'})

    def test_the_builder_reproduces_the_committed_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db = tmp / 'bench.db'
            votes = [{k: v for k, v in one.items() if k != 'career'} for one in self.export]
            make_bench_db(db, {'reviewer.one': votes})
            ranked = sorted(self.fixture['careers'], key=lambda career: self.fixture['careers'][career]['rank'])
            blind_key = {one['item_id']: {'career': one['career']} for one in self.export
                         if not one['item_id'].startswith('S') and one['career']}
            make_shape_review_dir(tmp / 'review', ranked, blind_key)
            call_command('build_review_judgements', db=str(db), shape_review_dir=str(tmp / 'review'),
                         reviewer='reviewer.one', output=str(tmp / 'fixture.json'), stdout=StringIO())
            rebuilt = json.loads((tmp / 'fixture.json').read_text())
            self.assertEqual((tmp / 'fixture.votes.json').read_bytes(), self.export_path.read_bytes())

        for found in (rebuilt, self.fixture):
            found['built_from'].pop('sources')
            found['built_from'].pop('database')
        self.assertEqual(rebuilt, self.fixture)

    def test_the_export_serialises_canonically(self):
        self.assertEqual(votes_export_bytes(self.export), self.export_path.read_bytes())


class ScoreAgainstJudgementsCommandTests(SimpleTestCase):
    """The scoring command over a replay checkpoint, with a small fixture."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        fixture = fixture_for([
            vote('S01-1-intro', 'good', [(1, INTRO, 'good')]),
            vote('S02-1-intro', 'needs_work', [(1, INTRO, 'bad')], drops=[1]),
        ], calibration_careers=1)
        self.fixture = self.dir / 'fixture.json'
        self.fixture.write_text(json.dumps(fixture))
        replay = [run(ALPHA, [variant('shape_pick_v2:2/0/0', [course('good', INTRO), course('x', INTRO)])]),
                  run(BETA, [variant('shape_pick_v2:2/0/0', [course('bad', INTRO), course('y', INTRO)]),
                             variant('shape_cut:2/0/0', [course('z', INTRO), course('y', INTRO)])])]
        self.replay = self.dir / 'replay.jsonl'
        self.replay.write_text(''.join(json.dumps(r) + '\n' for r in replay))

    def tearDown(self):
        self.tmp.cleanup()

    def test_scores_and_writes_rows(self):
        out = StringIO()
        call_command('score_against_judgements', input=[f'eco={self.replay}'], label=['shape_pick_v2:*'],
                     by_split=True, fixture=str(self.fixture), output_json=str(self.dir / 'rows.json'), stdout=out)
        rows = json.loads((self.dir / 'rows.json').read_text())['rows']
        summary = [(r['input'], r['group'], r['split'], r['pathways'], r['rejected_rate'], r['clean_rate'])
                   for r in rows]
        self.assertEqual(summary, [
            ('eco', 'shape_pick_v2:2/0/0', 'all', 2, 0.5, 0.5),
            ('eco', 'shape_pick_v2:2/0/0', 'calibration', 1, 0.0, 1.0),
            ('eco', 'shape_pick_v2:2/0/0', 'held_out', 1, 1.0, 0.0),
        ])
        self.assertIn('shape_pick_v2:2/0/0', out.getvalue())

    def test_group_by_tier_and_a_missing_input(self):
        out = StringIO()
        call_command('score_against_judgements', input=[str(self.replay)], group_by='tier',
                     fixture=str(self.fixture), stdout=out)
        self.assertIn('replay.jsonl', out.getvalue())
        self.assertIn('intro', out.getvalue())
        with self.assertRaises(CommandError):
            call_command('score_against_judgements', input=[str(self.dir / 'missing.jsonl')],
                         fixture=str(self.fixture), stdout=StringIO())
