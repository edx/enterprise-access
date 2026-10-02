"""
Evaluation personas and their expected results, as version-controlled data.

A persona is one learner intake payload plus the answer an expert would give for it.
Personas are the harness's ground truth, so this module's job is less "parse YAML" than
"refuse to load ground truth that cannot be scored against".

Three validation rules carry their weight:

* **Inputs must satisfy the real request contract.** ``inputs`` is validated by
  ``LearningIntentRequestSerializer`` itself, not a copy of its rules, so a persona that
  the live endpoint would reject can never enter a run.
* **Expected courses are Algolia catalog keys, never titles.** The defect being measured
  is literally "same title, different key", so a title cannot identify a course. Titles
  are still allowed *alongside* the key, as a human-readable note.
* **Expected careers are Lightcast ``external_id`` values.** Career titles are neither
  unique nor stable in the taxonomy index.

The catalog key format is enforced strictly because getting it wrong is silent: a
``course-v1:...`` *run* key looks like a course identifier, but the catalog index's
``key`` field holds the course key (``HarvardX+ER22.1x``), so run keys match nothing and
score as a miss no matter how good retrieval is.
"""
import re
from datetime import date
from pathlib import Path
from typing import Any

import attrs
import yaml
from django.conf import settings

from enterprise_access.apps.api.serializers.learner_pathways import LearningIntentRequestSerializer
from enterprise_access.apps.pathways.content_keys import is_course_run_key, is_valid_course_key

# Default location of the version-controlled persona set.
PERSONA_FIXTURE_DIR = Path(__file__).parent / 'fixtures' / 'personas'

# Lightcast job identifiers, e.g. "ETEA2F329D54D4142E".
LIGHTCAST_EXTERNAL_ID_PATTERN = re.compile(r'^ET[0-9A-F]{16}$')

# The complete persona schema. Enforced strictly, because an unrecognised key is almost
# always a misspelt one -- and a persona whose ground truth silently failed to load has no
# expected courses, so it scores 0% and reads exactly like a retrieval failure. That is the
# worst way for a typo to present in an evaluation.
PERSONA_KEYS = frozenset({
    'id', 'domain', 'tier', 'inputs', 'expected', 'catalog', 'notes',
})
EXPECTED_KEYS = frozenset({
    'careers', 'courses', 'expect_no_coverage', 'ground_truth_status',
})

# ``tier`` marks how much catalog coverage a persona is expected to have. ``edge``
# personas are drawn from known-thin domains on purpose.
PERSONA_TIERS = frozenset({'core', 'edge'})

# Whether a persona's expectations are real ground truth yet.
#
# Authoring ground truth is expert work and the long pole of the whole evaluation, so the
# harness has to run before it is finished. That makes "is this a real expectation?" a
# question reports must be able to answer mechanically -- a comment in a YAML file cannot
# keep a placeholder out of a headline recall number.
GROUND_TRUTH_EXPERT_AUTHORED = 'expert_authored'
GROUND_TRUTH_PLACEHOLDER = 'placeholder'
GROUND_TRUTH_STATUSES = frozenset({GROUND_TRUTH_EXPERT_AUTHORED, GROUND_TRUTH_PLACEHOLDER})


class PersonaValidationError(Exception):
    """
    Raised when a persona file cannot be loaded as scoreable ground truth.

    Always names the offending persona and field: these files are authored by hand by
    people who are not looking at this code, so the message is the whole interface.
    """


@attrs.frozen
class ExpectedCourse:
    """One course an expert says belongs in this persona's pathway."""

    key: str
    title: str | None = None
    note: str | None = None


@attrs.frozen
class ExpectedCareer:
    """One career an expert says this persona's intake should surface."""

    external_id: str
    name: str | None = None


@attrs.frozen
class PersonaCatalogContext:
    """
    Which catalog the ground truth was authored against.

    Both fields matter for the same reason: expected courses are only meaningful
    relative to one enterprise's catalog at one point in time. The taxonomy took
    thousands of skill updates in a single month, so a persona with no snapshot date
    cannot be told apart from one whose answers have simply gone stale.
    """

    enterprise_uuid: str | None = None
    snapshot_date: date | None = None


@attrs.frozen
class Persona:
    """
    One evaluation persona: an intake payload plus its expected results.
    """

    id: str
    domain: str
    tier: str
    inputs: dict[str, str]
    expected_careers: tuple[ExpectedCareer, ...] = ()
    expected_courses: tuple[ExpectedCourse, ...] = ()
    expect_no_coverage: bool = False
    ground_truth_status: str = GROUND_TRUTH_PLACEHOLDER
    catalog: PersonaCatalogContext = attrs.field(factory=PersonaCatalogContext)
    notes: str | None = None
    source_path: Path | None = None

    @property
    def is_expert_authored(self) -> bool:
        """Whether this persona's expectations may drive a reported metric."""
        return self.ground_truth_status == GROUND_TRUTH_EXPERT_AUTHORED

    @property
    def is_technology(self) -> bool:
        """
        Whether this persona counts toward the technology split.

        The technology / non-technology delta is the sharpest quality signal available,
        so the split is derived from one declared field rather than inferred per report.
        """
        return self.domain == 'technology'

    @property
    def expected_course_keys(self) -> tuple[str, ...]:
        return tuple(course.key for course in self.expected_courses)

    @property
    def expected_career_ids(self) -> tuple[str, ...]:
        return tuple(career.external_id for career in self.expected_careers)

    @property
    def has_ground_truth(self) -> bool:
        """
        Whether this persona can contribute to a recall metric.

        A persona marked ``expect_no_coverage`` is scoreable *because* it has no expected
        courses -- absence is the expected result. A persona with neither expected courses
        nor that flag is simply unfinished, and reports must be able to say so rather
        than counting it as a failure.
        """
        return bool(self.expected_courses) or self.expect_no_coverage


def _require_mapping(value: Any, persona_id: str, field_name: str) -> dict:
    """Assert that a persona sub-structure is a mapping, naming it if it is not."""
    if not isinstance(value, dict):
        raise PersonaValidationError(
            f'Persona {persona_id!r}: {field_name} must be a mapping, got {type(value).__name__}.'
        )
    return value


def _validate_inputs(raw_inputs: Any, persona_id: str) -> dict[str, str]:
    """
    Validate ``inputs`` against the live learning-intent request contract.

    Delegating to the serializer is deliberate: if the endpoint's contract changes, every
    persona fails loudly here rather than at run time, three hundred model calls in.
    """
    inputs = _require_mapping(raw_inputs, persona_id, 'inputs')
    serializer = LearningIntentRequestSerializer(data=inputs)
    if not serializer.is_valid():
        raise PersonaValidationError(
            f'Persona {persona_id!r}: inputs do not satisfy LearningIntentRequestSerializer: '
            f'{serializer.errors}'
        )
    return dict(serializer.validated_data)


def _validate_expected_courses(raw_courses: Any, persona_id: str) -> tuple[ExpectedCourse, ...]:
    """
    Coerce and validate the expected-course list, rejecting anything that is not a key.
    """
    if raw_courses is None:
        return ()
    if not isinstance(raw_courses, list):
        raise PersonaValidationError(
            f'Persona {persona_id!r}: expected.courses must be a list.'
        )

    courses = []
    for position, entry in enumerate(raw_courses):
        location = f'expected.courses[{position}]'

        if isinstance(entry, str):
            raise PersonaValidationError(
                f'Persona {persona_id!r}: {location} is a bare string ({entry!r}). '
                'Expected courses must be given as a mapping with a "key", because course '
                'titles are not unique -- duplicate titles under different keys are one of '
                'the defects being measured.'
            )

        entry = _require_mapping(entry, persona_id, location)
        key = (entry.get('key') or '').strip()
        title = entry.get('title')

        if not key:
            titled = f' (title: {title!r})' if title else ''
            raise PersonaValidationError(
                f'Persona {persona_id!r}: {location} has no "key"{titled}. '
                'Ground truth must be recorded as Algolia catalog course keys, not titles.'
            )
        if is_course_run_key(key):
            raise PersonaValidationError(
                f'Persona {persona_id!r}: {location} key {key!r} is a course *run* key. '
                'The Algolia catalog index keys courses as "<org>+<number>" '
                '(e.g. "HarvardX+ER22.1x"), so a run key can never match a hit.'
            )
        if not is_valid_course_key(key):
            raise PersonaValidationError(
                f'Persona {persona_id!r}: {location} key {key!r} is not a valid catalog '
                'course key. Expected "<org>+<number>", e.g. "IBM+DA0101EN".'
            )

        courses.append(ExpectedCourse(
            key=key,
            title=title,
            note=entry.get('note'),
        ))

    return tuple(courses)


def _validate_expected_careers(raw_careers: Any, persona_id: str) -> tuple[ExpectedCareer, ...]:
    """
    Coerce and validate the expected-career list, rejecting anything that is not an
    ``external_id``.
    """
    if raw_careers is None:
        return ()
    if not isinstance(raw_careers, list):
        raise PersonaValidationError(
            f'Persona {persona_id!r}: expected.careers must be a list.'
        )

    careers = []
    for position, entry in enumerate(raw_careers):
        location = f'expected.careers[{position}]'

        if isinstance(entry, str):
            raise PersonaValidationError(
                f'Persona {persona_id!r}: {location} is a bare string ({entry!r}). '
                'Expected careers must be given as a mapping with an "external_id", '
                'because career titles are neither unique nor stable in the taxonomy index.'
            )

        entry = _require_mapping(entry, persona_id, location)
        external_id = (entry.get('external_id') or '').strip()
        name = entry.get('name')

        if not external_id:
            named = f' (name: {name!r})' if name else ''
            raise PersonaValidationError(
                f'Persona {persona_id!r}: {location} has no "external_id"{named}. '
                'Ground truth must be recorded as Lightcast external_ids, not career titles.'
            )
        if not LIGHTCAST_EXTERNAL_ID_PATTERN.match(external_id):
            raise PersonaValidationError(
                f'Persona {persona_id!r}: {location} external_id {external_id!r} does not '
                'look like a Lightcast job id (expected "ET" followed by 16 hex digits).'
            )

        careers.append(ExpectedCareer(external_id=external_id, name=name))

    return tuple(careers)


def _validate_catalog_context(raw_catalog: Any, persona_id: str) -> PersonaCatalogContext:
    """Validate the optional catalog-provenance block."""
    if raw_catalog is None:
        return PersonaCatalogContext()

    catalog = _require_mapping(raw_catalog, persona_id, 'catalog')
    snapshot_date = catalog.get('snapshot_date')
    if snapshot_date is not None and not isinstance(snapshot_date, date):
        raise PersonaValidationError(
            f'Persona {persona_id!r}: catalog.snapshot_date must be a YAML date '
            f'(YYYY-MM-DD), got {snapshot_date!r}.'
        )

    enterprise_uuid = catalog.get('enterprise_uuid')
    return PersonaCatalogContext(
        enterprise_uuid=str(enterprise_uuid) if enterprise_uuid else None,
        snapshot_date=snapshot_date,
    )


def _reject_unknown_keys(mapping: dict, allowed: frozenset, persona_id: str, where: str) -> None:
    """
    Fail on any key outside the schema.

    Deliberately strict rather than forgiving. ``expected_courses`` at the top level
    instead of ``expected.courses`` is a plausible mistake, and ignoring it would produce
    a persona with no ground truth that scores zero -- indistinguishable from a genuine
    total retrieval failure.
    """
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise PersonaValidationError(
            f'Persona {persona_id!r}: unrecognised {where} key(s) {unknown}. '
            f'Allowed: {sorted(allowed)}. A silently ignored key would leave this persona '
            'with no ground truth, which scores zero and looks like a retrieval failure.'
        )


def persona_from_dict(data: Any, source_path: Path | None = None) -> Persona:
    """
    Build and validate one ``Persona`` from parsed YAML.

    Raises:
        PersonaValidationError: If the persona is not scoreable ground truth.
    """
    where = str(source_path) if source_path else '<inline>'
    if not isinstance(data, dict):
        raise PersonaValidationError(f'{where}: persona file must contain a YAML mapping.')

    persona_id = (data.get('id') or '').strip()
    if not persona_id:
        raise PersonaValidationError(f'{where}: persona is missing a non-empty "id".')

    domain = (data.get('domain') or '').strip()
    if not domain:
        raise PersonaValidationError(f'Persona {persona_id!r}: "domain" is required.')

    tier = (data.get('tier') or 'core').strip()
    if tier not in PERSONA_TIERS:
        raise PersonaValidationError(
            f'Persona {persona_id!r}: tier {tier!r} is not one of {sorted(PERSONA_TIERS)}.'
        )

    _reject_unknown_keys(data, PERSONA_KEYS, persona_id, 'persona')

    expected = data.get('expected') or {}
    expected = _require_mapping(expected, persona_id, 'expected')
    _reject_unknown_keys(expected, EXPECTED_KEYS, persona_id, 'expected')

    ground_truth_status = (expected.get('ground_truth_status') or GROUND_TRUTH_PLACEHOLDER).strip()
    if ground_truth_status not in GROUND_TRUTH_STATUSES:
        raise PersonaValidationError(
            f'Persona {persona_id!r}: expected.ground_truth_status {ground_truth_status!r} is '
            f'not one of {sorted(GROUND_TRUTH_STATUSES)}.'
        )

    expect_no_coverage = bool(expected.get('expect_no_coverage', False))
    expected_courses = _validate_expected_courses(expected.get('courses'), persona_id)

    if expect_no_coverage and expected_courses:
        raise PersonaValidationError(
            f'Persona {persona_id!r}: expect_no_coverage is true but {len(expected_courses)} '
            'expected course(s) are listed. A persona cannot both be uncoverable and have '
            'a correct answer.'
        )

    return Persona(
        id=persona_id,
        domain=domain,
        tier=tier,
        inputs=_validate_inputs(data.get('inputs'), persona_id),
        expected_careers=_validate_expected_careers(expected.get('careers'), persona_id),
        expected_courses=expected_courses,
        expect_no_coverage=expect_no_coverage,
        ground_truth_status=ground_truth_status,
        catalog=_validate_catalog_context(data.get('catalog'), persona_id),
        notes=data.get('notes'),
        source_path=source_path,
    )


def load_persona_file(path: Path) -> Persona:
    """
    Load and validate a single persona YAML file.

    Raises:
        PersonaValidationError: If the file is unparseable or not scoreable.
    """
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PersonaValidationError(f'{path}: could not parse YAML: {exc}') from exc
    return persona_from_dict(raw, source_path=path)


def load_personas(
    fixture_dir: Path | str | None = None,
    persona_ids: list[str] | None = None,
) -> list[Persona]:
    """
    Load every persona in ``fixture_dir``, sorted by persona id.

    Args:
        fixture_dir: Directory of ``*.yaml`` persona files. Defaults to
            ``settings.PATHWAY_EVAL_PERSONA_DIR`` when set, else the bundled fixtures.
        persona_ids: Optional allow-list. Every requested id must exist, so a typo in a
            run invocation fails instead of silently scoring a smaller set.

    Raises:
        PersonaValidationError: If the directory is missing, any persona is invalid,
            two personas share an id, or a requested id is absent.
    """
    resolved_dir = Path(
        fixture_dir or
        getattr(settings, 'PATHWAY_EVAL_PERSONA_DIR', None) or
        PERSONA_FIXTURE_DIR
    )
    if not resolved_dir.is_dir():
        raise PersonaValidationError(f'Persona fixture directory does not exist: {resolved_dir}')

    personas: dict[str, Persona] = {}
    for path in sorted(resolved_dir.glob('*.yaml')):
        persona = load_persona_file(path)
        if persona.id in personas:
            raise PersonaValidationError(
                f'Duplicate persona id {persona.id!r} in {path} '
                f'(already defined by {personas[persona.id].source_path}).'
            )
        personas[persona.id] = persona

    if persona_ids is not None:
        missing = [persona_id for persona_id in persona_ids if persona_id not in personas]
        if missing:
            raise PersonaValidationError(
                f'No persona found for id(s): {", ".join(sorted(missing))}. '
                f'Available: {", ".join(sorted(personas)) or "(none)"}.'
            )
        return [personas[persona_id] for persona_id in persona_ids]

    return [personas[persona_id] for persona_id in sorted(personas)]
