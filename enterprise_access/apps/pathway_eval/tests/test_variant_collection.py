"""
Tests for batch pathway-variant collection.

As with the harness, the budget and dry-run tests are correctness properties: this command
can spend real money in a loop. The lookup tests matter for a different reason -- a
nearest-neighbour match would quietly collect data for a career nobody asked about.
"""
import csv
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from enterprise_access.apps.pathway_eval.variant_collection import (
    NO_CAREER,
    CareerRun,
    VariantCollector,
    append_checkpoint,
    load_career_names,
    load_checkpoint,
    lookup_career,
    summarise,
    write_csv
)
from enterprise_access.apps.workflow.exceptions import UnitOfWorkException

PATCH_WORKFLOW = 'enterprise_access.apps.pathway_eval.variant_collection.PathwayAssemblyWorkflow'
PATCH_CLIENT = 'enterprise_access.apps.pathway_eval.variant_collection.AlgoliaSearchClient'
PATCH_LOOKUP = 'enterprise_access.apps.pathway_eval.variant_collection.lookup_career'

CAREER = {'external_id': 'ET1', 'name': 'Data Analyst', 'skills': ['SQL', 'Excel'], 'industries': []}


def assembly(keys=('A+1', 'B+1', 'C+1', 'D+1', 'E+1'), complete=True):
    return {
        'courses': [{'key': key, 'title': f'Course {key}'} for key in keys],
        'complete': complete,
        'level_mix': {'Introductory': 3, 'Intermediate': 2, 'Advanced': 0},
        'violations': [],
    }


def variant(label, keys, verdict=None, same_as=''):
    strategy, _, size = label.partition(':')
    return {
        'label': label, 'strategy': strategy,
        'requested_size': None if '-' in size else int(size),
        'courses': [{'key': key, 'title': key} for key in keys],
        'complete': True, 'level_mix': {'Introductory': len(keys)}, 'violations': [],
        'error': '',
        'judgement': {'verdict': verdict, 'n_on_topic': 2, 'reason': 'r', 'same_as': same_as}
        if verdict else None,
    }


def fake_workflow_class(output=None, variants=(), judgement=None, error=None):
    """A stand-in PathwayAssemblyWorkflow class."""
    instance = mock.Mock()
    instance.uuid = 'wf-uuid'
    instance.output_data = {'assemble_pathway_output': output or assembly()}
    instance.variants.return_value = list(variants)
    instance.default_judgement.return_value = judgement
    if error:
        instance.execute.side_effect = error
    cls = mock.Mock()
    cls.objects.create.return_value = instance
    cls.generate_input_dict.return_value = {}
    return cls, instance


class TestLoadCareerNames(TestCase):
    """
    Scenario: Careers come from a plain list or a spreadsheet export.
    """

    def _write(self, text):
        """Write ``text`` to a temporary file and return its path."""
        handle = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False, encoding='utf-8')
        handle.write(text)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return handle.name

    def test_a_plain_list_skips_blanks_comments_and_repeats(self):
        path = self._write('Data Analyst\n\n# not this\nWelder\nData Analyst\n')

        self.assertEqual(load_career_names(path), ['Data Analyst', 'Welder'])

    def test_the_best_served_careers_tab_export_is_read_by_its_career_column(self):
        path = self._write('Family rank,Content area,Career family,Career\n'
                           '1,Business,Project Manager,IT Project Manager\n'
                           '1,Business,Project Manager,"Project Manager, Senior"\n')

        self.assertEqual(load_career_names(path), ['IT Project Manager', 'Project Manager, Senior'])

    def test_a_csv_with_a_career_name_column_is_read(self):
        path = self._write('career_name,notes\nWelder,x\n')

        self.assertEqual(load_career_names(path), ['Welder'])

    def test_an_empty_file_yields_nothing(self):
        self.assertEqual(load_career_names(self._write('')), [])


class TestLookupCareer(TestCase):
    """
    Scenario: A career is found by its exact name, never a neighbour.
    """

    @mock.patch(PATCH_CLIENT)
    def test_the_exact_name_is_matched_case_insensitively(self, mock_client):
        mock_search = mock_client.return_value.search_jobs_index
        mock_search.return_value = {'hits': [
            {'external_id': 'ET0', 'name': 'Senior Data Analyst', 'skills': [{'name': 'SQL'}]},
            {'external_id': 'ET1', 'name': 'Data Analyst', 'skills': [{'name': 'SQL'}]},
        ]}

        career = lookup_career('data analyst')

        self.assertEqual(career['external_id'], 'ET1')
        self.assertEqual(mock_search.call_args.args[0], '')
        filters = mock_search.call_args.kwargs['filters']
        self.assertIn('metadata_language:en', filters)
        self.assertIn('name:"data analyst"', filters)

    @mock.patch(PATCH_CLIENT)
    def test_quotes_in_a_name_are_escaped_in_the_filter(self, mock_client):
        mock_search = mock_client.return_value.search_jobs_index
        mock_search.return_value = {'hits': []}

        lookup_career('The "Fixer"')

        self.assertIn('name:"The \\"Fixer\\""', mock_search.call_args.kwargs['filters'])

    @mock.patch(PATCH_CLIENT)
    def test_a_match_with_skills_is_preferred(self, mock_client):
        mock_client.return_value.search_jobs_index.return_value = {'hits': [
            {'external_id': 'ET1', 'name': 'Welder', 'skills': []},
            {'external_id': 'ET2', 'name': 'Welder', 'skills': [{'name': 'Welding'}]},
        ]}

        self.assertEqual(lookup_career('Welder')['external_id'], 'ET2')

    @mock.patch(PATCH_CLIENT)
    def test_no_exact_match_is_none_rather_than_the_nearest(self, mock_client):
        mock_client.return_value.search_jobs_index.return_value = {'hits': [
            {'external_id': 'ET0', 'name': 'Senior Data Analyst', 'skills': [{'name': 'SQL'}]},
        ]}

        self.assertIsNone(lookup_career('Data Analyst'))


class TestVariantCollector(TestCase):
    """
    Scenario: Careers run through the workflow with their experiments, within budget.
    """

    def _collector(self, **kwargs):
        kwargs.setdefault('lookup', lambda name: dict(CAREER, name=name))
        return VariantCollector(**kwargs)

    def test_the_cost_bound_counts_rerank_arms_judge_and_enrichment(self):
        collector = self._collector(variant_strategies=['model_pick'], variant_sizes=[2, 3],
                                    judge_enabled=True, enrich_enabled=True)

        # rerank 1 + model_pick 2 + judge (1 default + 2 variants) 3 + enrich 1
        self.assertEqual(collector.calls_per_career, 7)

    def test_a_dry_run_issues_nothing(self):
        cls, _ = fake_workflow_class()
        with mock.patch(PATCH_WORKFLOW, cls):
            result = self._collector(dry_run=True).run(['Welder'])

        cls.objects.create.assert_not_called()
        self.assertEqual(result['runs'][0].skipped_reason, 'dry run')
        self.assertEqual(result['calls_charged'], 0)

    def test_the_budget_refuses_a_career_it_cannot_afford_whole(self):
        cls, _ = fake_workflow_class()
        collector = self._collector(variant_strategies=['model_sized'], max_calls=3)  # 2 per career
        with mock.patch(PATCH_WORKFLOW, cls):
            result = collector.run(['A', 'B'])

        self.assertEqual(cls.objects.create.call_count, 1)
        self.assertEqual(result['runs'][1].skipped_reason, 'max calls reached')
        self.assertTrue(result['budget_exhausted'])
        self.assertLessEqual(result['calls_charged'], 3)

    def test_a_career_skipped_at_lookup_is_not_charged(self):
        cls, _ = fake_workflow_class()
        collector = self._collector(variant_strategies=['model_sized'], max_calls=2,
                                    lookup=lambda name: None if name == 'Nobody' else dict(CAREER))
        with mock.patch(PATCH_WORKFLOW, cls):
            result = collector.run(['Nobody', 'Welder'])

        self.assertEqual(result['calls_charged'], 2)
        self.assertEqual(cls.objects.create.call_count, 1)
        self.assertFalse(result['budget_exhausted'])

    def test_the_experiment_request_reaches_the_workflow(self):
        cls, _ = fake_workflow_class()
        collector = self._collector(variant_sizes=[4, 2], judge_enabled=True, rerank_enabled=False)
        with mock.patch(PATCH_WORKFLOW, cls):
            collector.run(['Welder'])

        kwargs = cls.generate_input_dict.call_args.kwargs
        self.assertEqual(kwargs['career_skills'], ['SQL', 'Excel'])
        self.assertEqual(kwargs['variant_sizes'], [2, 4])
        self.assertEqual(kwargs['variant_strategies'], ['ranked_cut'])
        self.assertTrue(kwargs['judge_enabled'])
        self.assertFalse(kwargs['rerank_enabled'])
        self.assertFalse(kwargs['enrich_enabled'])

    def test_the_run_records_the_pathway_its_judgement_and_the_variants(self):
        cls, _ = fake_workflow_class(
            variants=[variant('ranked_cut:2', ['A+1', 'B+1'], verdict='good')],
            judgement={'label': 'default', 'verdict': 'weak'},
        )
        with mock.patch(PATCH_WORKFLOW, cls):
            run = self._collector(variant_sizes=[2]).run(['Welder'])['runs'][0]

        self.assertEqual(run.workflow_uuid, 'wf-uuid')
        self.assertEqual(len(run.pathway['courses']), 5)
        self.assertEqual(run.judgement['verdict'], 'weak')
        self.assertEqual(run.variants[0]['label'], 'ranked_cut:2')

    def test_an_unknown_or_skill_less_career_is_skipped(self):
        cls, _ = fake_workflow_class()
        collector = self._collector(
            lookup=lambda name: None if name == 'Nobody' else dict(CAREER, skills=[]),
        )
        with mock.patch(PATCH_WORKFLOW, cls):
            runs = collector.run(['Nobody', 'Skill-less'])['runs']

        self.assertIn('exact name', runs[0].skipped_reason)
        self.assertEqual(runs[1].skipped_reason, 'career carries no skills')
        cls.objects.create.assert_not_called()

    def test_a_failure_costs_one_career_not_the_batch(self):
        failing, _ = fake_workflow_class(error=UnitOfWorkException('retrieval broke'))

        def lookup(name):
            if name == 'Broken lookup':
                raise ConnectionError('algolia down')
            return dict(CAREER, name=name)

        with mock.patch(PATCH_WORKFLOW, failing):
            runs = self._collector(lookup=lookup).run(['Broken lookup', 'Welder'])['runs']

        self.assertIn('ConnectionError', runs[0].error)
        self.assertIn('retrieval broke', runs[1].error)


class TestCheckpointAndResume(TestCase):
    """
    Scenario: An interrupted collection keeps what it paid for, and resumes past it.

    Found necessary on the first real run, which hung on a network call 40 minutes in.
    """

    def setUp(self):
        super().setUp()
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.path = Path(tmpdir.name) / 'checkpoint.jsonl'

    def test_final_runs_are_those_worth_keeping(self):
        self.assertTrue(CareerRun('a', pathway=assembly()).is_final)
        self.assertTrue(CareerRun('a', skipped_reason=NO_CAREER).is_final)
        self.assertFalse(CareerRun('a', error='boom').is_final)
        self.assertFalse(CareerRun('a', skipped_reason='max calls reached').is_final)
        self.assertFalse(CareerRun('a', skipped_reason='dry run').is_final)

    def test_a_run_round_trips_through_the_checkpoint(self):
        run = CareerRun('Welder', career_name='Welder', pathway=assembly(),
                        variants=[variant('ranked_cut:2', ['A+1', 'B+1'], 'good')])
        append_checkpoint(self.path, run)

        restored = load_checkpoint(self.path)['Welder']

        self.assertEqual(restored.to_dict(), run.to_dict())

    def test_errors_are_not_resumed_and_later_lines_win(self):
        append_checkpoint(self.path, CareerRun('A', error='timeout'))
        append_checkpoint(self.path, CareerRun('B', pathway=assembly(keys=('X+1',))))
        append_checkpoint(self.path, CareerRun('B', pathway=assembly(keys=('Y+1',))))

        done = load_checkpoint(self.path)

        self.assertNotIn('A', done)
        self.assertEqual(done['B'].pathway['courses'][0]['key'], 'Y+1')

    def test_a_torn_last_line_is_skipped_not_fatal(self):
        append_checkpoint(self.path, CareerRun('A', pathway=assembly()))
        with open(self.path, 'a', encoding='utf-8') as handle:
            handle.write('{"requested_name": "B", "pathw')

        self.assertEqual(list(load_checkpoint(self.path)), ['A'])

    def test_a_missing_checkpoint_resumes_nothing(self):
        self.assertEqual(load_checkpoint(self.path), {})

    def test_each_completed_career_is_handed_over_as_it_finishes(self):
        cls, _ = fake_workflow_class()
        seen = []
        collector = VariantCollector(lookup=lambda name: None if name == 'Nobody' else dict(CAREER))
        with mock.patch(PATCH_WORKFLOW, cls):
            collector.run(['Welder', 'Nobody'], on_run=seen.append)

        self.assertEqual([run.requested_name for run in seen], ['Welder', 'Nobody'])

    def test_resumed_careers_are_carried_over_not_rerun_or_charged(self):
        cls, _ = fake_workflow_class()
        prior = CareerRun('Welder', career_name='Welder', pathway=assembly())
        collector = VariantCollector(lookup=lambda name: dict(CAREER, name=name))
        with mock.patch(PATCH_WORKFLOW, cls):
            result = collector.run(['Welder', 'Data Analyst'], done={'Welder': prior})

        self.assertIs(result['runs'][0], prior)
        self.assertEqual(result['resumed'], 1)
        self.assertEqual(cls.objects.create.call_count, 1)
        self.assertEqual(result['calls_charged'], collector.calls_per_career)


class TestExports(TestCase):
    """
    Scenario: Every pathway lands in one flat row beside its baseline.
    """

    def _run(self):
        return CareerRun(
            requested_name='data analyst', career_name='Data Analyst', external_id='ET1',
            workflow_uuid='wf', pathway=assembly(),
            judgement={'label': 'default', 'verdict': 'good', 'n_on_topic': 5, 'reason': 'All fit.'},
            variants=[
                variant('ranked_cut:2', ['A+1', 'B+1'], verdict='good'),
                variant('model_sized:2-5', ['A+1', 'C+1', 'D+1']),
            ],
        )

    def test_rows_start_with_the_delivered_pathway(self):
        rows = self._run().pathway_rows()

        self.assertEqual([row['label'] for row in rows], ['default', 'ranked_cut:2', 'model_sized:2-5'])
        self.assertEqual(rows[0]['size'], 5)
        self.assertEqual(rows[0]['level_mix'], '3/2/0')
        self.assertEqual(rows[1]['course_keys'], 'A+1 | B+1')
        self.assertEqual(rows[2]['requested_size'], '')
        self.assertEqual(rows[2]['verdict'], '')

    def test_the_summary_counts_verdicts_per_label_and_keeps_unjudged_apart(self):
        summary = {row['label']: row for row in summarise([self._run(), self._run()])}

        self.assertEqual(summary['default']['good'], 2)
        self.assertEqual(summary['model_sized:2-5']['unjudged'], 2)
        self.assertEqual(summary['ranked_cut:2']['courses'], 4)
        self.assertEqual(list(summary)[0], 'default')

    def test_the_csv_has_one_row_per_pathway(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'variants.csv'
            count = write_csv([self._run()], path)
            rows = list(csv.DictReader(path.open(encoding='utf-8')))

        self.assertEqual(count, 3)
        self.assertEqual(rows[1]['verdict'], 'good')


class TestCollectPathwayVariantsCommand(TestCase):
    """
    Tests for the ``collect_pathway_variants`` command.
    """

    def call(self, **kwargs):
        stdout = StringIO()
        call_command('collect_pathway_variants', stdout=stdout, **kwargs)
        return stdout.getvalue()

    def test_no_careers_is_a_command_error(self):
        with self.assertRaisesRegex(CommandError, 'No careers'):
            self.call(dry_run=True)

    def test_a_bad_variant_size_is_a_command_error(self):
        with self.assertRaisesRegex(CommandError, 'between 2 and 5'):
            self.call(career=['Welder'], variant_sizes=[1], dry_run=True)

    def test_a_dry_run_reports_the_plan_and_its_cost_bound(self):
        output = self.call(career=['Welder', 'Data Analyst'], variant_strategies=['model_pick'],
                           judge=True, dry_run=True)

        # rerank 1 + model_pick 4 + judge 5 = 10 per career.
        self.assertIn('DRY RUN', output)
        self.assertIn('up to 10 paid model call(s) per career; 20 for the whole list', output)
        self.assertIn('SKIPPED  (dry run)', output)

    def test_resume_without_a_checkpoint_is_a_command_error(self):
        with self.assertRaisesRegex(CommandError, '--resume needs --checkpoint'):
            self.call(career=['Welder'], resume=True, dry_run=True)

    def test_a_checkpointed_collection_resumes_where_it_stopped(self):
        cls, _ = fake_workflow_class()
        with tempfile.TemporaryDirectory() as tmpdir, \
                mock.patch(PATCH_WORKFLOW, cls), \
                mock.patch(PATCH_LOOKUP, side_effect=lambda name: dict(CAREER, name=name)):
            checkpoint = Path(tmpdir) / 'nested' / 'checkpoint.jsonl'
            self.call(career=['Welder'], checkpoint=str(checkpoint))
            output = self.call(career=['Welder', 'Data Analyst'], checkpoint=str(checkpoint),
                               resume=True, output_json=str(Path(tmpdir) / 'runs.json'))
            payload = json.loads((Path(tmpdir) / 'runs.json').read_text())
            lines = checkpoint.read_text().splitlines()

        self.assertEqual(cls.objects.create.call_count, 2)
        self.assertIn('resumed from checkpoint (not re-run, not charged): 1', output)
        self.assertEqual([run['requested_name'] for run in payload['runs']], ['Welder', 'Data Analyst'])
        self.assertEqual(len(lines), 2)

    def test_a_dry_run_writes_no_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            checkpoint = Path(tmpdir) / 'checkpoint.jsonl'
            self.call(career=['Welder'], checkpoint=str(checkpoint), dry_run=True)

            self.assertFalse(checkpoint.exists())

    def test_limit_trims_the_list(self):
        output = self.call(career=['A', 'B', 'C'], limit=2, dry_run=True)

        self.assertIn('careers: 2', output)

    def test_runs_are_exported_as_json_and_csv(self):
        cls, _ = fake_workflow_class(variants=[variant('ranked_cut:3', ['A+1', 'B+1', 'C+1'], 'good')])
        with tempfile.TemporaryDirectory() as tmpdir, \
                mock.patch(PATCH_WORKFLOW, cls), \
                mock.patch(PATCH_LOOKUP, return_value=CAREER):
            json_path, csv_path = Path(tmpdir) / 'out' / 'runs.json', Path(tmpdir) / 'rows.csv'
            output = self.call(career=['Data Analyst'], variant_sizes=[3],
                               output_json=str(json_path), output_csv=str(csv_path))
            payload = json.loads(json_path.read_text())
            rows = list(csv.DictReader(csv_path.open(encoding='utf-8')))

        self.assertEqual(payload['run_config']['variant_strategies'], ['ranked_cut'])
        self.assertEqual(payload['runs'][0]['career_name'], 'Data Analyst')
        self.assertEqual([row['label'] for row in rows], ['default', 'ranked_cut:3'])
        self.assertIn('ranked_cut:3', output)
