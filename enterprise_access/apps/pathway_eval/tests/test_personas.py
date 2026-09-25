"""
Tests for persona fixture loading and validation.
"""
import tempfile
from datetime import date
from pathlib import Path

import ddt
import yaml
from django.test import TestCase, override_settings

from enterprise_access.apps.pathway_eval.personas import (
    GROUND_TRUTH_EXPERT_AUTHORED,
    GROUND_TRUTH_PLACEHOLDER,
    PERSONA_FIXTURE_DIR,
    PersonaValidationError,
    load_persona_file,
    load_personas,
    persona_from_dict
)

VALID_INPUTS = {
    'selected_goals': 'Move into a data analyst role',
    'free_text': 'I do spreadsheet reporting and want to work with real databases.',
    'known_context': 'Comfortable with Excel, no programming background.',
    'interested_industries': 'Finance and Insurance',
}


def make_persona_dict(**overrides):
    """A minimal valid persona payload, with ``expected`` merged rather than replaced."""
    persona = {
        'id': 'p999-test',
        'domain': 'technology',
        'tier': 'core',
        'inputs': dict(VALID_INPUTS),
        'expected': {
            'ground_truth_status': GROUND_TRUTH_EXPERT_AUTHORED,
            'careers': [{'external_id': 'ETE78CD2CDFFFAC66B', 'name': 'Data Analyst Consultant'}],
            'courses': [{'key': 'IBM+DA0101EN', 'title': 'Analyzing Data with Python'}],
        },
    }
    expected_overrides = overrides.pop('expected', None)
    persona.update(overrides)
    if expected_overrides is not None:
        persona['expected'] = {**persona['expected'], **expected_overrides}
    return persona


@ddt.ddt
class TestPersonaFromDict(TestCase):
    """
    Tests for ``persona_from_dict`` validation.
    """

    def test_a_persona_round_trips(self):
        """Scenario: A persona round-trips."""
        persona = persona_from_dict(make_persona_dict())

        self.assertEqual(persona.id, 'p999-test')
        self.assertEqual(persona.domain, 'technology')
        self.assertEqual(persona.tier, 'core')
        # Inputs come back as the serializer validated them, so they are directly
        # postable to the learning-intent endpoint.
        self.assertEqual(persona.inputs, VALID_INPUTS)
        self.assertEqual(persona.expected_course_keys, ('IBM+DA0101EN',))
        self.assertEqual(persona.expected_career_ids, ('ETE78CD2CDFFFAC66B',))
        self.assertTrue(persona.is_technology)
        self.assertTrue(persona.has_ground_truth)
        self.assertTrue(persona.is_expert_authored)

    # -- the schema is closed, because a misspelt key scores zero ---------------------

    @ddt.data(
        'expected_courses',
        'expected_careers',
        'ground_truth_status',
        'expcted',
    )
    def test_an_unrecognised_top_level_key_is_rejected(self, bad_key):
        """
        ``expected_courses`` at the top level instead of ``expected.courses`` is a
        plausible mistake. Ignoring it would leave the persona with no ground truth, which
        scores 0% and reads exactly like a total retrieval failure.
        """
        payload = make_persona_dict()
        payload[bad_key] = []

        with self.assertRaisesRegex(PersonaValidationError, 'unrecognised persona key'):
            persona_from_dict(payload)

    def test_an_unrecognised_expected_key_is_rejected(self):
        payload = make_persona_dict()
        payload['expected']['course'] = [{'key': 'IBM+DA0101EN'}]

        with self.assertRaisesRegex(PersonaValidationError, 'unrecognised expected key'):
            persona_from_dict(payload)

    def test_the_documented_schema_is_accepted_in_full(self):
        """Guards against the closed schema being narrower than the shipped fixtures."""
        payload = make_persona_dict(
            catalog={'enterprise_uuid': '417306cb-b24a-4d06-b83c-fb2a61d7fb96'},
            notes='a note',
        )

        persona = persona_from_dict(payload)

        self.assertEqual(persona.notes, 'a note')

    # -- inputs must satisfy the real request contract --------------------------------

    @ddt.data(
        'selected_goals',
        'free_text',
        'known_context',
        'interested_industries',
    )
    def test_missing_required_input_field_is_rejected(self, field_name):
        inputs = dict(VALID_INPUTS)
        del inputs[field_name]

        with self.assertRaisesRegex(PersonaValidationError, 'LearningIntentRequestSerializer'):
            persona_from_dict(make_persona_dict(inputs=inputs))

    @ddt.data('', '   ')
    def test_blank_input_field_is_rejected(self, blank_value):
        inputs = dict(VALID_INPUTS, free_text=blank_value)

        with self.assertRaisesRegex(PersonaValidationError, 'LearningIntentRequestSerializer'):
            persona_from_dict(make_persona_dict(inputs=inputs))

    @ddt.data(None, 'a string', ['a', 'list'])
    def test_non_mapping_inputs_is_rejected(self, inputs):
        with self.assertRaisesRegex(PersonaValidationError, 'inputs must be a mapping'):
            persona_from_dict(make_persona_dict(inputs=inputs))

    # -- ground truth uses identifiers, not titles ------------------------------------

    def test_course_given_as_a_title_only_is_rejected_naming_the_entry(self):
        """Scenario: Ground truth uses identifiers not titles."""
        persona_dict = make_persona_dict(
            expected={'courses': [{'title': 'Analyzing Data with Python'}]},
        )

        with self.assertRaises(PersonaValidationError) as ctx:
            persona_from_dict(persona_dict)

        message = str(ctx.exception)
        self.assertIn('expected.courses[0]', message)
        self.assertIn('Analyzing Data with Python', message)
        self.assertIn('not titles', message)

    def test_course_given_as_a_bare_string_is_rejected(self):
        persona_dict = make_persona_dict(
            expected={'courses': ['Analyzing Data with Python']},
        )

        with self.assertRaises(PersonaValidationError) as ctx:
            persona_from_dict(persona_dict)

        self.assertIn('bare string', str(ctx.exception))
        self.assertIn('Analyzing Data with Python', str(ctx.exception))

    def test_course_run_key_is_rejected_with_an_explanation(self):
        """
        A ``course-v1:`` run key is the plausible-looking wrong answer: valid elsewhere
        on the platform, absent from the catalog index, and therefore a guaranteed miss.
        """
        persona_dict = make_persona_dict(
            expected={'courses': [{'key': 'course-v1:HarvardX+ER22.1x+2T2019'}]},
        )

        with self.assertRaises(PersonaValidationError) as ctx:
            persona_from_dict(persona_dict)

        message = str(ctx.exception)
        self.assertIn('course *run* key', message)
        self.assertIn('<org>+<number>', message)

    @ddt.data(
        'IBM+DA0101EN',
        'HarvardX+ER22.1x',
        'CodeSignal+164',
        'MGH_Institute+MGH-RN101',
        'StanfordOnline+SOM-YCME0045',
    )
    def test_real_catalog_course_keys_are_accepted(self, course_key):
        """These are verbatim ``key`` values observed in the catalog index."""
        persona = persona_from_dict(
            make_persona_dict(expected={'courses': [{'key': course_key}]}),
        )

        self.assertEqual(persona.expected_course_keys, (course_key,))

    @ddt.data('no-plus-sign', '+missing-org', 'missing-number+', 'has space+X1')
    def test_malformed_course_keys_are_rejected(self, course_key):
        with self.assertRaisesRegex(PersonaValidationError, 'not a valid catalog'):
            persona_from_dict(make_persona_dict(expected={'courses': [{'key': course_key}]}))

    def test_career_given_as_a_name_only_is_rejected(self):
        persona_dict = make_persona_dict(
            expected={'careers': [{'name': 'Data Analyst'}]},
        )

        with self.assertRaises(PersonaValidationError) as ctx:
            persona_from_dict(persona_dict)

        message = str(ctx.exception)
        self.assertIn('expected.careers[0]', message)
        self.assertIn('Data Analyst', message)
        self.assertIn('not career titles', message)

    @ddt.data('Data Analyst', 'ET123', 'etea2f329d54d4142e', 'XXEA2F329D54D4142E')
    def test_malformed_career_ids_are_rejected(self, external_id):
        with self.assertRaises(PersonaValidationError):
            persona_from_dict(make_persona_dict(expected={'careers': [{'external_id': external_id}]}))

    # -- known-uncoverable personas ---------------------------------------------------

    def test_expect_no_coverage_is_preserved_and_scoreable(self):
        """Scenario: Known-uncoverable personas are marked."""
        persona = persona_from_dict(make_persona_dict(
            expected={'expect_no_coverage': True, 'courses': []},
        ))

        self.assertTrue(persona.expect_no_coverage)
        self.assertEqual(persona.expected_courses, ())
        # Absence *is* the expected answer, so this persona is scoreable with no
        # expected courses -- scorers must not treat it as unfinished.
        self.assertTrue(persona.has_ground_truth)

    def test_expect_no_coverage_with_expected_courses_is_contradictory(self):
        with self.assertRaisesRegex(PersonaValidationError, 'cannot both be uncoverable'):
            persona_from_dict(make_persona_dict(
                expected={'expect_no_coverage': True, 'courses': [{'key': 'IBM+DA0101EN'}]},
            ))

    def test_persona_without_courses_or_the_flag_is_unfinished_not_invalid(self):
        """
        Ground-truth authoring is unfinished by design at this stage. Such a persona must
        load -- so it can be listed and chased -- while reporting no ground truth.
        """
        persona = persona_from_dict(make_persona_dict(
            expected={'expect_no_coverage': False, 'courses': []},
        ))

        self.assertFalse(persona.has_ground_truth)

    # -- placeholder vs expert-authored -----------------------------------------------

    def test_ground_truth_status_defaults_to_placeholder(self):
        persona_dict = make_persona_dict()
        del persona_dict['expected']['ground_truth_status']

        persona = persona_from_dict(persona_dict)

        self.assertEqual(persona.ground_truth_status, GROUND_TRUTH_PLACEHOLDER)
        self.assertFalse(persona.is_expert_authored)

    def test_unknown_ground_truth_status_is_rejected(self):
        with self.assertRaisesRegex(PersonaValidationError, 'ground_truth_status'):
            persona_from_dict(make_persona_dict(expected={'ground_truth_status': 'probably-fine'}))

    # -- required identity fields -----------------------------------------------------

    @ddt.data(
        ({'id': ''}, 'non-empty "id"'),
        ({'domain': ''}, '"domain" is required'),
        ({'tier': 'medium'}, 'is not one of'),
    )
    @ddt.unpack
    def test_identity_field_validation(self, overrides, expected_message):
        persona_dict = make_persona_dict(**overrides)

        with self.assertRaisesRegex(PersonaValidationError, expected_message):
            persona_from_dict(persona_dict)

    def test_non_mapping_persona_is_rejected(self):
        with self.assertRaisesRegex(PersonaValidationError, 'must contain a YAML mapping'):
            persona_from_dict(['not', 'a', 'mapping'])

    # -- catalog provenance -----------------------------------------------------------

    def test_catalog_context_is_parsed(self):
        persona = persona_from_dict(make_persona_dict(catalog={
            'enterprise_uuid': '11111111-2222-3333-4444-555555555555',
            'snapshot_date': date(2026, 9, 9),
        }))

        self.assertEqual(persona.catalog.enterprise_uuid, '11111111-2222-3333-4444-555555555555')
        self.assertEqual(persona.catalog.snapshot_date, date(2026, 9, 9))

    def test_string_snapshot_date_is_rejected(self):
        """YAML parses bare ``2026-09-09`` as a date; a quoted string is an authoring slip."""
        with self.assertRaisesRegex(PersonaValidationError, 'snapshot_date must be a YAML date'):
            persona_from_dict(make_persona_dict(catalog={'snapshot_date': '2026-09-09'}))


class TestLoadPersonas(TestCase):
    """
    Tests for loading persona files off disk.
    """

    def _write_persona(self, directory, filename, persona_dict):
        path = Path(directory) / filename
        path.write_text(yaml.safe_dump(persona_dict, sort_keys=False))
        return path

    def test_load_persona_file_records_its_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_persona(tmpdir, 'p999-test.yaml', make_persona_dict())

            persona = load_persona_file(path)

            self.assertEqual(persona.source_path, path)

    def test_unparseable_yaml_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'broken.yaml'
            path.write_text('id: p1\n  bad: indentation\n:::\n')

            with self.assertRaisesRegex(PersonaValidationError, 'could not parse YAML'):
                load_persona_file(path)

    def test_duplicate_persona_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_persona(tmpdir, 'a.yaml', make_persona_dict())
            self._write_persona(tmpdir, 'b.yaml', make_persona_dict())

            with self.assertRaisesRegex(PersonaValidationError, 'Duplicate persona id'):
                load_personas(fixture_dir=tmpdir)

    def test_missing_directory_is_rejected(self):
        with self.assertRaisesRegex(PersonaValidationError, 'does not exist'):
            load_personas(fixture_dir='/nonexistent/persona/dir')

    def test_persona_ids_filter_requires_every_requested_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_persona(tmpdir, 'a.yaml', make_persona_dict(id='p001-a'))

            with self.assertRaisesRegex(PersonaValidationError, 'p002-typo'):
                load_personas(fixture_dir=tmpdir, persona_ids=['p001-a', 'p002-typo'])

    def test_persona_ids_filter_preserves_requested_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_persona(tmpdir, 'a.yaml', make_persona_dict(id='p001-a'))
            self._write_persona(tmpdir, 'b.yaml', make_persona_dict(id='p002-b'))

            personas = load_personas(fixture_dir=tmpdir, persona_ids=['p002-b', 'p001-a'])

            self.assertEqual([p.id for p in personas], ['p002-b', 'p001-a'])

    def test_settings_override_selects_the_persona_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_persona(tmpdir, 'a.yaml', make_persona_dict(id='p001-from-settings'))

            with override_settings(PATHWAY_EVAL_PERSONA_DIR=tmpdir):
                personas = load_personas()

            self.assertEqual([p.id for p in personas], ['p001-from-settings'])


class TestBundledPersonaFixtures(TestCase):
    """
    The persona set that actually ships is loaded and checked here, so an authoring
    slip in a YAML file fails CI rather than a run.
    """

    def setUp(self):
        super().setUp()
        self.personas = load_personas(fixture_dir=PERSONA_FIXTURE_DIR)

    def test_bundled_personas_all_load(self):
        self.assertGreater(len(self.personas), 0)
        self.assertEqual(
            [p.id for p in self.personas],
            sorted(p.id for p in self.personas),
        )

    def test_the_set_covers_both_sides_of_the_technology_split(self):
        domains = {p.domain for p in self.personas}

        self.assertIn('technology', domains)
        self.assertTrue(domains - {'technology'}, 'the set has no non-technology personas')

    def test_the_set_includes_a_known_uncoverable_persona(self):
        """The diagnostic's third outcome has to be represented, not stumbled into."""
        uncoverable = [p for p in self.personas if p.expect_no_coverage]

        self.assertTrue(uncoverable, 'no persona exercises the expect_no_coverage path')
        for persona in uncoverable:
            self.assertEqual(persona.tier, 'edge')

    def test_every_bundled_persona_declares_a_snapshot_date(self):
        """Without one, a stale expectation is indistinguishable from a regression."""
        for persona in self.personas:
            with self.subTest(persona=persona.id):
                self.assertIsNotNone(persona.catalog.snapshot_date)
