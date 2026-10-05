"""
Tests for collecting a career family under pooled skills (``--families-file``).

The failure that matters is a silent one: a family that is meant to be searched with its
members' skills quietly falling back to its namesake career's own, which is the very list the
pooling exists to replace. So a malformed families file is an error, and every run records where
its skills came from.
"""
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from enterprise_access.apps.pathway_eval.tests.test_variant_collection import fake_workflow_class
from enterprise_access.apps.pathway_eval.variant_collection import (
    SKILLS_OWN,
    SKILLS_POOLED,
    CareerRun,
    VariantCollector,
    family_lookup,
    load_families,
    lookup_career_hit
)

PATCH_WORKFLOW = 'enterprise_access.apps.pathway_eval.variant_collection.PathwayAssemblyWorkflow'
PATCH_CLIENT = 'enterprise_access.apps.pathway_eval.variant_collection.AlgoliaSearchClient'
PATCH_HIT = 'enterprise_access.apps.pathway_eval.variant_collection.lookup_career_hit'
PATCH_LOOKUP = 'enterprise_access.apps.pathway_eval.variant_collection.lookup_career'


def raw_hit(name, *skills, external_id=None, description=''):
    """A raw jobs-index hit, its skills given as ``(name, unique_postings)``."""
    return {
        'external_id': external_id or f'ET-{name}', 'name': name, 'description': description,
        'skills': [{'name': skill, 'unique_postings': postings} for skill, postings in skills],
    }


HITS = {
    'Field Development Representative': raw_hit(
        'Field Development Representative', ('Veterinary Pathology', 51), ('Animal Science', 117),
        description='Engages potential clients in a territory.',
    ),
    'Sales Development Representative': raw_hit(
        'Sales Development Representative', ('Lead Generation', 2000), ('Sales Process', 900),
    ),
    'Business Development Representative': raw_hit(
        'Business Development Representative', ('Lead Generation', 700), ('Business Development', 600),
    ),
}

FAMILY = {
    'family': 'Field Development Representative',
    'career': 'Field Development Representative',
    'members': ['Field Development Representative', 'Sales Development Representative',
                'Business Development Representative', 'Nonexistent Representative'],
}


def write_json(tmpdir, payload, name='families.json'):
    path = Path(tmpdir) / name
    path.write_text(json.dumps(payload))
    return path


class TestLookupCareerHit(TestCase):
    """
    Scenario: The raw record is found by exact name, as the career candidate is.
    """

    @mock.patch(PATCH_CLIENT)
    def test_the_raw_record_keeps_each_skills_posting_count(self, mock_client):
        mock_client.return_value.search_jobs_index.return_value = {'hits': [
            raw_hit('Senior Welder', ('Welding', 9)), raw_hit('Welder', ('Welding', 40)),
        ]}

        found = lookup_career_hit('welder')

        self.assertEqual(found['name'], 'Welder')
        self.assertEqual(found['skills'][0]['unique_postings'], 40)

    @mock.patch(PATCH_CLIENT)
    def test_a_record_with_skills_is_preferred_and_a_neighbour_is_never_taken(self, mock_client):
        mock_client.return_value.search_jobs_index.return_value = {'hits': [
            raw_hit('Welder', external_id='ET1'), raw_hit('Welder', ('Welding', 1), external_id='ET2'),
        ]}
        self.assertEqual(lookup_career_hit('Welder')['external_id'], 'ET2')

        mock_client.return_value.search_jobs_index.return_value = {'hits': [raw_hit('Senior Welder')]}
        self.assertIsNone(lookup_career_hit('Welder'))


class TestLoadFamilies(TestCase):
    """
    Scenario: A families file is read strictly, so a typo cannot fall back to own skills.
    """

    def test_a_valid_file_is_keyed_by_family(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            families = load_families(write_json(tmpdir, [
                {'family': ' Security Engineer ', 'career': 'Security Engineer',
                 'members': ['Security Engineer', 'Security Architect', 'Security Architect', ' ']},
            ]))

        self.assertEqual(families, {'Security Engineer': {
            'family': 'Security Engineer', 'career': 'Security Engineer',
            'members': ['Security Engineer', 'Security Architect'],
        }})

    def test_malformed_files_are_refused(self):
        cases = {
            'not a list': {'family': 'X'},
            'not an object': ['X'],
            'no members': [{'family': 'X', 'career': 'X', 'members': []}],
            'no career': [{'family': 'X', 'members': ['X']}],
            'a name that is not text': [{'family': 7, 'career': 'X', 'members': ['X']}],
            'named twice': [{'family': 'X', 'career': 'X', 'members': ['X']}] * 2,
        }
        for label, payload in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmpdir:
                with self.assertRaises(ValueError):
                    load_families(write_json(tmpdir, payload))


class TestFamilyLookup(TestCase):
    """
    Scenario: A listed family keeps its career's name and description but searches with the pool.
    """

    def lookup(self, **kwargs):
        kwargs.setdefault('fetch_hit', HITS.get)
        return family_lookup({FAMILY['family']: FAMILY}, **kwargs)

    def test_a_family_is_searched_with_its_members_pooled_skills(self):
        career = self.lookup()('Field Development Representative')

        self.assertEqual((career['name'], career['external_id']),
                         ('Field Development Representative', 'ET-Field Development Representative'))
        self.assertEqual(career['description'], 'Engages potential clients in a territory.')
        self.assertEqual(career['skills'][:3], ['Lead Generation', 'Sales Process', 'Business Development'])
        self.assertEqual(career['skill_source'], SKILLS_POOLED)
        self.assertEqual(career['members'], FAMILY['members'][:3])
        self.assertEqual(career['members_missing'], ['Nonexistent Representative'])

    def test_a_career_not_listed_falls_back_to_its_own_skills(self):
        fallback = mock.Mock(return_value={'name': 'Welder', 'skills': ['Welding']})

        self.assertEqual(self.lookup(fallback=fallback)('Welder'), {'name': 'Welder', 'skills': ['Welding']})
        fallback.assert_called_once_with('Welder')

    def test_a_family_whose_career_is_not_in_the_index_is_not_found(self):
        self.assertIsNone(self.lookup(fetch_hit=lambda name: None)('Field Development Representative'))


class TestCollectorRecordsTheSkillSource(TestCase):
    """
    Scenario: Every run says whether it searched with its own skills or a family's pool.
    """

    def test_pooled_and_own_runs_are_told_apart_and_round_trip(self):
        welder = {'external_id': 'ET1', 'name': 'Welder', 'skills': ['Welding'], 'industries': []}
        lookup = family_lookup({FAMILY['family']: FAMILY}, fetch_hit=HITS.get, fallback=lambda name: dict(welder))
        cls, _ = fake_workflow_class()
        with mock.patch(PATCH_WORKFLOW, cls):
            runs = VariantCollector(lookup=lookup).run(['Field Development Representative', 'Welder'])['runs']

        family_run, own_run = runs[0], runs[1]
        self.assertEqual(family_run.skill_source, SKILLS_POOLED)
        self.assertEqual(family_run.career_skills[0], 'Lead Generation')
        self.assertEqual(family_run.members_missing, ['Nonexistent Representative'])
        self.assertEqual(cls.generate_input_dict.call_args_list[0].kwargs['career_skills'][0], 'Lead Generation')
        self.assertEqual((own_run.skill_source, own_run.members), (SKILLS_OWN, []))
        self.assertEqual(CareerRun.from_dict(family_run.to_dict()), family_run)


class TestCollectCommandFamiliesFile(TestCase):
    """
    Scenario: ``--families-file`` reaches the collector, and a bad file stops the command.
    """

    def call(self, **kwargs):
        stdout = StringIO()
        call_command('collect_pathway_variants', stdout=stdout, **kwargs)
        return stdout.getvalue()

    def test_a_listed_family_is_collected_with_pooled_skills(self):
        cls, _ = fake_workflow_class()
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch(PATCH_WORKFLOW, cls), \
                mock.patch(PATCH_HIT, side_effect=HITS.get):
            families_path = write_json(tmpdir, [FAMILY])
            json_path = Path(tmpdir) / 'runs.json'
            self.call(career=['Field Development Representative'], families_file=str(families_path),
                      output_json=str(json_path))
            run = json.loads(json_path.read_text())['runs'][0]

        self.assertEqual(run['skill_source'], SKILLS_POOLED)
        self.assertEqual(run['career_skills'][:2], ['Lead Generation', 'Sales Process'])
        self.assertEqual(cls.generate_input_dict.call_args.kwargs['career_skills'][0], 'Lead Generation')

    def test_without_a_families_file_a_career_keeps_its_own_skills(self):
        cls, _ = fake_workflow_class()
        career = {'external_id': 'ET1', 'name': 'Welder', 'skills': ['Welding'], 'industries': []}
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch(PATCH_WORKFLOW, cls), \
                mock.patch(PATCH_LOOKUP, return_value=career):
            json_path = Path(tmpdir) / 'runs.json'
            self.call(career=['Welder'], output_json=str(json_path))
            run = json.loads(json_path.read_text())['runs'][0]

        self.assertEqual((run['skill_source'], run['career_skills']), (SKILLS_OWN, ['Welding']))

    def test_an_unreadable_or_malformed_families_file_is_a_command_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            malformed = write_json(tmpdir, [{'family': 'X'}])
            for path in (Path(tmpdir) / 'missing.json', malformed):
                with self.subTest(path=path.name):
                    with self.assertRaisesRegex(CommandError, 'Could not read --families-file'):
                        self.call(career=['X'], families_file=str(path), dry_run=True)
