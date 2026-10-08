"""
The retrieval diagnostic: does Algolia surface the courses an expert would pick?

This answers one question, and the answer decides where the pipeline's quality problem
actually lives:

============================  ==================================================
Result                        What it means
============================  ==================================================
Expected course in the top N  Retrieval works. Re-ranking is the right fix.
Present but not in the top N  The query is wrong, not the ranking. Fix intent
                              and query construction; re-ranking would be
                              polish on the wrong layer.
Not findable at all           Content coverage, not engineering.
============================  ==================================================

Deliberately does **not** call Xpert. Intent extraction is a separate stage with its own
failure modes, and mixing it in here would mean a bad result could always be blamed on
the prompt. Instead the diagnostic issues several *fixed* query strategies built directly
from the persona, and reports each one. If no simple strategy retrieves the expected
course, that is a stronger finding than one prompt underperforming; if a simple strategy
does, that bounds how much intent extraction can be worth.

Known limitation on absence
---------------------------
A search-only Algolia key cannot enumerate an index: it has no ``browse`` ACL, and
pagination is capped at 1000 hits (the catalog index holds ~4,100 courses). So absence is
established by *probe* -- a targeted title search -- not by enumeration, and the outcome
is named ``NOT_FOUND_IN_INDEX`` rather than "not in the catalog". Turning that into a
definitive answer needs either a browse-scoped key or enterprise-catalog's
``contains_content_items``.
"""
import logging
import uuid as uuid_module
from dataclasses import dataclass, field
from typing import Any

from enterprise_access.apps.api_client.algolia_client import AlgoliaClientError, AlgoliaSearchClient
from enterprise_access.apps.pathway_eval.personas import Persona

logger = logging.getLogger(__name__)

# How many hits count as "retrieved". The quality doc's diagnostic is a top-20 question.
DEFAULT_TOP_N = 20

# Hits requested when probing whether a specific course exists in the index at all.
PROBE_HITS_PER_PAGE = 50

# Free text is a paragraph; Algolia queries are not. Truncated at a word boundary.
MAX_QUERY_CHARS = 200

# Attributes the diagnostic needs back. Kept minimal: this runs once per persona per
# strategy and the payload is otherwise dominated by descriptions.
COURSE_ATTRIBUTES = ['key', 'title', 'level_type', 'partners']

COURSE_SCOPE_FILTER = 'content_type:course'

# Scoping the diagnostic to one enterprise customer needs no secured key:
# ``enterprise_customer_uuids`` is a facetable attribute, so it can be filtered with the
# plain search key. (It is also in the index's ``unretrievableAttributes``, which hides it
# from a hit but does not block filtering or faceting on it.) That matters because a
# secured key is vended per request from a user token and so cannot be obtained by a
# management command at all -- see ``docs/references/algolia_search.md``.
CUSTOMER_SCOPE_FACET = 'enterprise_customer_uuids'

# The catalog index ANDs every query word, and has no ``removeWordsIfNoResults``
# configured. Measured against the production index on 2026-09-09, for a single persona's
# goal text: 4 words -> 90 hits, 5 -> 4, 6 -> 1, 8 or more -> 0. Sending
# ``removeWordsIfNoResults=allOptional`` turned the same 24-word query from 0 hits into
# 348, and a 5-word career title from 0 into 121.
#
# This matters more than it looks: a verbose query does not return *bad* results, it
# returns *none*, which is what makes the POC's retrieval ladder descend to its widest
# step. Passing this parameter is therefore a candidate fix that the diagnostic has to be
# able to measure rather than assume, so it is a run-time option and not a constant.
RELAXED_QUERY_PARAMS = {'removeWordsIfNoResults': 'allOptional'}


def validate_customer_uuid(customer_uuid):
    """
    Return ``customer_uuid`` normalised, or raise ``ValueError``.

    Module-level so a caller can reject a bad argument *before* building an Algolia
    client: a malformed UUID is an input error and should not need working credentials to
    surface.

    Algolia does not error on a filter that matches nothing, so an unvalidated typo would
    not fail -- it would return zero hits for every persona and report 0% recall, which is
    indistinguishable from a genuine total retrieval failure.
    """
    if customer_uuid is None:
        return None
    try:
        return str(uuid_module.UUID(str(customer_uuid)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(
            f'{customer_uuid!r} is not a UUID. An enterprise customer scope has to be a '
            'UUID; a malformed one would silently match no courses and report 0% recall '
            'rather than failing.'
        ) from exc


class Outcome:
    """The mutually exclusive conclusions the diagnostic can reach about a persona."""

    NO_GROUND_TRUTH = 'no_ground_truth'
    EXPECTED_NO_COVERAGE = 'expected_no_coverage'
    IN_TOP_N = 'in_top_n'
    NOT_IN_TOP_N = 'not_in_top_n'
    NOT_FOUND_IN_INDEX = 'not_found_in_index'

    #: Maps each outcome to the decision it implies, per the gate table.
    CONSEQUENCES = {
        NO_GROUND_TRUTH: 'Unscoreable -- ground truth not authored yet.',
        EXPECTED_NO_COVERAGE: 'Absence is the expected result; verify nothing plausible was returned.',
        IN_TOP_N: 'Retrieval is fine. Re-ranking is the right fix.',
        NOT_IN_TOP_N: 'Problem is upstream: intent and query construction, not ranking.',
        NOT_FOUND_IN_INDEX: 'Content coverage, not engineering. Re-scope to servable domains.',
    }


def _truncate_query(text: str) -> str:
    """Shorten a query to ``MAX_QUERY_CHARS`` without splitting a word."""
    collapsed = ' '.join((text or '').split())
    if len(collapsed) <= MAX_QUERY_CHARS:
        return collapsed
    return collapsed[:MAX_QUERY_CHARS].rsplit(' ', 1)[0]


def build_query_strategies(persona: Persona) -> dict[str, str]:
    """
    Build the fixed set of queries to try for one persona.

    Each is a plausible, *deterministic* stand-in for what intent extraction produces.
    Reporting all of them is what separates "the index cannot surface this" from "our
    particular query cannot surface this".

    Returns a mapping of strategy name to query text, skipping strategies with no text.
    """
    inputs = persona.inputs
    strategies = {
        # What the learner literally said they want.
        'goals_only': inputs.get('selected_goals', ''),
        # Goals plus the free-text elaboration: the most information available without
        # a model in the loop.
        'goals_and_free_text': f"{inputs.get('selected_goals', '')} {inputs.get('free_text', '')}",
        # The POC's own last-resort query, and a useful floor.
        'career_title': ' '.join(
            career.name for career in persona.expected_careers if career.name
        ),
    }
    return {
        name: _truncate_query(text)
        for name, text in strategies.items()
        if _truncate_query(text)
    }


@dataclass
class StrategyResult:
    """The outcome of one query strategy for one persona."""

    strategy: str
    query: str
    returned_keys: list[str] = field(default_factory=list)
    #: Expected course key -> 1-based rank within the returned hits.
    matched_ranks: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def best_rank(self) -> int | None:
        """Rank of the first expected course retrieved, or ``None`` if none were."""
        return min(self.matched_ranks.values()) if self.matched_ranks else None


@dataclass
class PersonaDiagnostic:
    """Everything the diagnostic concluded about one persona."""

    persona_id: str
    domain: str
    tier: str
    is_technology: bool
    ground_truth_status: str
    expected_course_keys: list[str] = field(default_factory=list)
    strategy_results: list[StrategyResult] = field(default_factory=list)
    #: Expected key -> whether a targeted probe found it in the index at all.
    probe_found: dict[str, bool] = field(default_factory=dict)
    outcome: str = Outcome.NO_GROUND_TRUTH
    #: For expect_no_coverage personas: what the index returned anyway, for eyeballing.
    incidental_hits: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def best_rank(self) -> int | None:
        """Best rank achieved by any strategy, or ``None`` if nothing was retrieved."""
        ranks = [
            result.best_rank
            for result in self.strategy_results
            if result.best_rank is not None
        ]
        return min(ranks) if ranks else None

    @property
    def retrieved_keys(self) -> set[str]:
        """Expected keys retrieved by at least one strategy."""
        retrieved = set()
        for result in self.strategy_results:
            retrieved.update(result.matched_ranks)
        return retrieved

    @property
    def recall_at_top_n(self) -> float | None:
        """Fraction of expected courses retrieved by at least one strategy."""
        if not self.expected_course_keys:
            return None
        return len(self.retrieved_keys) / len(self.expected_course_keys)


class RetrievalDiagnostic:
    """
    Runs the diagnostic over a persona set.

    Holds no pipeline logic: it issues plain Algolia searches and compares keys. The
    Algolia client is injected so tests never touch the network.
    """

    def __init__(
        self,
        algolia_client: AlgoliaSearchClient | None = None,
        top_n: int = DEFAULT_TOP_N,
        allow_unscoped: bool = False,
        secured_key=None,
        relax_query: bool = False,
        customer_uuid: str | None = None,
    ):
        self.client = algolia_client or AlgoliaSearchClient()
        self.top_n = top_n
        self.allow_unscoped = allow_unscoped
        self.secured_key = secured_key
        self.relax_query = relax_query
        self.customer_uuid = validate_customer_uuid(customer_uuid)

    @property
    def catalog_filters(self) -> str:
        """The Algolia ``filters`` expression applied to every catalog query."""
        if self.customer_uuid is None:
            return COURSE_SCOPE_FILTER
        return f'{COURSE_SCOPE_FILTER} AND {CUSTOMER_SCOPE_FACET}:"{self.customer_uuid}"'

    def count_scoped_courses(self) -> int:
        """
        How many courses the current scope actually contains.

        Worth calling before a run: a UUID that is well-formed but wrong (a catalog UUID
        where a customer UUID belongs, say) returns zero, and zero courses in scope makes
        every recall number meaningless rather than bad.
        """
        response = self.client.search_catalog_index(
            '',
            secured_key=self.secured_key,
            allow_unscoped=self.allow_unscoped,
            filters=self.catalog_filters,
            hitsPerPage=0,
        )
        return response.get('nbHits', 0)

    @property
    def extra_search_params(self) -> dict[str, Any]:
        """Search parameters applied to every query in this run."""
        return dict(RELAXED_QUERY_PARAMS) if self.relax_query else {}

    def _search_catalog(self, query: str, hits_per_page: int) -> dict[str, Any]:
        return self.client.search_catalog_index(
            query,
            secured_key=self.secured_key,
            allow_unscoped=self.allow_unscoped,
            filters=self.catalog_filters,
            hitsPerPage=hits_per_page,
            attributesToRetrieve=COURSE_ATTRIBUTES,
            **self.extra_search_params,
        )

    def _run_strategy(self, strategy: str, query: str, expected_keys: list[str]) -> StrategyResult:
        """Issue one strategy's query and record where the expected courses landed."""
        result = StrategyResult(strategy=strategy, query=query)
        try:
            response = self._search_catalog(query, self.top_n)
        except AlgoliaClientError as exc:
            result.error = str(exc)
            logger.warning('Diagnostic strategy %r failed: %s', strategy, exc)
            return result

        result.returned_keys = [hit.get('key') for hit in response.get('hits', []) if hit.get('key')]
        expected = set(expected_keys)
        for rank, key in enumerate(result.returned_keys, start=1):
            if key in expected and key not in result.matched_ranks:
                result.matched_ranks[key] = rank
        return result

    def _probe_for_course(self, course_key: str, title: str | None) -> bool:
        """
        Ask whether one specific course is findable in the index at all.

        Searches its title, because the catalog index makes ``key`` neither searchable nor
        filterable. A false result means "not findable by title probe", which is weaker
        than proven absence -- see the module docstring.
        """
        if not title:
            return False
        try:
            response = self._search_catalog(_truncate_query(title), PROBE_HITS_PER_PAGE)
        except AlgoliaClientError as exc:
            logger.warning('Probe for %r failed: %s', course_key, exc)
            return False
        return any(hit.get('key') == course_key for hit in response.get('hits', []))

    def _classify(self, diagnostic: PersonaDiagnostic, persona: Persona) -> str:
        """Reduce a persona's results to exactly one outcome."""
        if persona.expect_no_coverage:
            return Outcome.EXPECTED_NO_COVERAGE
        if not persona.expected_courses:
            return Outcome.NO_GROUND_TRUTH
        if diagnostic.retrieved_keys:
            return Outcome.IN_TOP_N
        # Nothing was retrieved. Whether that is a query problem or a coverage problem
        # depends on whether the courses exist in the index at all.
        if any(diagnostic.probe_found.values()):
            return Outcome.NOT_IN_TOP_N
        return Outcome.NOT_FOUND_IN_INDEX

    def run_for_persona(self, persona: Persona) -> PersonaDiagnostic:
        """Run every query strategy for one persona and classify the result."""
        diagnostic = PersonaDiagnostic(
            persona_id=persona.id,
            domain=persona.domain,
            tier=persona.tier,
            is_technology=persona.is_technology,
            ground_truth_status=persona.ground_truth_status,
            expected_course_keys=list(persona.expected_course_keys),
        )

        for strategy, query in build_query_strategies(persona).items():
            result = self._run_strategy(strategy, query, diagnostic.expected_course_keys)
            diagnostic.strategy_results.append(result)
            if result.error:
                diagnostic.errors.append(f'{strategy}: {result.error}')

        if persona.expect_no_coverage:
            # There is nothing to match, so record what came back instead. A pathway
            # built from these hits is padding, and a human needs to see it to agree.
            for result in diagnostic.strategy_results:
                for key in result.returned_keys[:5]:
                    diagnostic.incidental_hits.append({'strategy': result.strategy, 'key': key})
        else:
            unretrieved = [
                key for key in diagnostic.expected_course_keys
                if key not in diagnostic.retrieved_keys
            ]
            titles = {course.key: course.title for course in persona.expected_courses}
            for key in unretrieved:
                diagnostic.probe_found[key] = self._probe_for_course(key, titles.get(key))

        diagnostic.outcome = self._classify(diagnostic, persona)
        return diagnostic

    def run(self, personas: list[Persona]) -> list[PersonaDiagnostic]:
        return [self.run_for_persona(persona) for persona in personas]


def summarize(diagnostics: list[PersonaDiagnostic]) -> dict[str, Any]:
    """
    Aggregate per-persona results into the report the gate decision needs.

    Splits technology from non-technology because that delta is the sharpest quality
    signal available, and reports placeholder personas separately because scoring a
    guess as if it were expert judgement is worse than reporting nothing.
    """

    def split_stats(subset: list[PersonaDiagnostic]) -> dict[str, Any]:
        scoreable = [d for d in subset if d.recall_at_top_n is not None]
        recalls = [d.recall_at_top_n for d in scoreable]
        ranks = [d.best_rank for d in scoreable if d.best_rank is not None]
        return {
            'personas': len(subset),
            'scoreable': len(scoreable),
            'mean_recall_at_top_n': (sum(recalls) / len(recalls)) if recalls else None,
            'personas_with_any_expected_retrieved': len(ranks),
            'median_best_rank': sorted(ranks)[len(ranks) // 2] if ranks else None,
            'outcomes': {
                outcome: sum(1 for d in subset if d.outcome == outcome)
                for outcome in sorted({d.outcome for d in subset})
            },
        }

    expert_authored = [d for d in diagnostics if d.ground_truth_status == 'expert_authored']

    return {
        'total_personas': len(diagnostics),
        'expert_authored_personas': len(expert_authored),
        'placeholder_personas': len(diagnostics) - len(expert_authored),
        'overall': split_stats(diagnostics),
        'expert_authored_only': split_stats(expert_authored),
        'technology': split_stats([d for d in diagnostics if d.is_technology]),
        'non_technology': split_stats([d for d in diagnostics if not d.is_technology]),
        'errors': [error for d in diagnostics for error in d.errors],
    }
