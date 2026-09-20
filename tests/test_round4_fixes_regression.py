"""Regression tests for round 4's fixes (2026-09-20), covering the four
confirmed-live repro cases the founder asked to be pinned down so they can
never silently regress:

  TASK 1 -- claim-discipline: `resolve_citations()`/`verify_cite()` with no
    real claim/context raises a clear error instead of silently letting the
    citation string stand in for the claim (see `core/engine.py`'s module
    docstring and `core/models.py::ContextContract.__post_init__`). A
    DOI-only citation whose `doi` field matches a candidate exactly passes
    G6 identity immediately, without needing title-token overlap
    (`evidence/verifier.py::_exact_identifier_shortcut`); a DOI that does
    NOT match any candidate still correctly fails/falls through to the
    ordinary title/author check (and fails that too, for an unrelated
    candidate).

  TASK 2 -- evidence-status: the two exact repro cases from the external
    report (an OBJECTIVE-only passage, a HYPOTHESIS-only passage) now
    correctly classify as their statement type
    (`evidence/statement_type.py`) and the overall admission decision is
    HOLD, not ADMIT, even though shared-keyword overlap with the claim is
    high enough that the relation label alone would say SUPPORTS.

  TASK 3 -- proposition/relation: reduces/increases and protects/damages
    (previously-open semantic-opposite gaps, now closed by the round-4
    open word-CLASS mechanism in `evidence/relation.py`, not by adding two
    more pair-list entries) correctly flag a polarity mismatch using a
    claim/evidence pair NOT already covered by the closed
    `_DIRECTIONAL_PAIRS`/`_TIGHT_DIRECTIONAL_PAIRS` dictionaries. Also a
    genuine QUALIFIES case (narrower-scope evidence) maps to HOLD, not
    ADMIT.

  TASK 4 -- coverage-truth: a synthetic, stale ThaiJO local index (real
    ThaiIndex, real ThaiJOAdapter, no network) with its recorded sync
    timestamp aged past the staleness threshold reports STALE, not OK,
    end-to-end through `routing.router.route()` +
    `core.engine.discover_citations()` +
    `RouteDecision.update_coverage()`; a freshly-synced empty-result search
    reports SEARCHED_OK through the same real pipeline; an adapter that was
    routed to but never actually queried this run (no search ever
    executed) reports PLANNED, not OK.

No network, no mocking of the functions under test -- every fixture is
either a hand-authored OpenAlex-response-shaped JSON object (same technique
as `tests/test_g6_fix_offline.py`/`tests/test_cite_use_gate.py`) or a real
temp-file-backed `ThaiIndex` (same technique as
`tests/test_coverage_readout.py`/`tests/test_thaijo_index.py`).
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from thaicite.adapters.base import RawRecord
from thaicite.adapters.openalex import OpenAlexAdapter
from thaicite.adapters.thaijo import ThaiJOAdapter
from thaicite.adapters.thaijo_index import DEFAULT_STALE_THRESHOLD_S, ThaiIndex
from thaicite.core import coverage as cov
from thaicite.core.engine import discover_citations, resolve_citations
from thaicite.core.models import CanonicalWork, Decision, VerificationState
from thaicite.evidence.relation import classify_relation
from thaicite.evidence.statement_type import classify_statement_type
from thaicite.evidence.verifier import gate_admission_decision, gate_g6_identity_match, verify
from thaicite.mcp_server import verify_cite
from thaicite.routing.router import route

_ADAPTER = OpenAlexAdapter()


def _inverted_index(text: str) -> dict[str, list[int]]:
    index: dict[str, list[int]] = {}
    for pos, word in enumerate(text.split()):
        index.setdefault(word, []).append(pos)
    return index


def _openalex_work(*, work_id, title, authors, year, doi, abstract):
    return {
        "id": work_id,
        "title": title,
        "display_name": title,
        "publication_year": year,
        "doi": f"https://doi.org/{doi}",
        "authorships": [
            {"author": {"display_name": name}, "institutions": []} for name in authors
        ],
        "primary_location": {
            "landing_page_url": f"https://example.org/{work_id}",
            "source": {"display_name": "Example Venue", "issn": ["1234-5678"]},
        },
        "abstract_inverted_index": _inverted_index(abstract),
    }


def _candidate(work_json):
    record = RawRecord(source_record_id=work_json["id"], raw_metadata=work_json)
    return _ADAPTER.to_candidates([record])[0]


class _StubAdapter:
    """Same offline stub technique as `tests/test_cite_use_gate.py`."""

    name = "stub"

    def __init__(self, records):
        self._records = records

    def search(self, query):
        return self._records

    def to_candidates(self, result):
        return [_candidate(r) for r in result]


# ===========================================================================
# TASK 1 -- claim-discipline regression
# ===========================================================================


def test_resolve_citations_with_no_context_raises_instead_of_using_citation_as_claim():
    """`resolve_citations(context="", queries=[citation], ...)` must raise a
    clear `ValueError` -- it must NEVER silently let `citation` stand in as
    the claim (that would let a paper's own title/abstract trivially
    SUPPORTS its own title, per `core/engine.py`'s module docstring)."""
    adapter = _StubAdapter([])
    with pytest.raises(ValueError, match="claim"):
        resolve_citations(
            context="",
            queries=["Some Citation String (2020)"],
            adapters=[adapter],
        )


def test_resolve_citations_with_whitespace_only_context_raises():
    adapter = _StubAdapter([])
    with pytest.raises(ValueError, match="claim"):
        resolve_citations(context="   ", queries=["x"], adapters=[adapter])


def test_verify_cite_missing_context_returns_clear_error_not_a_traceback():
    """The MCP-facing `verify_cite()` catches the underlying `ValueError`
    and turns it into an actionable `error` field -- `verified` must stay
    False and no citation is ever fabricated from the bare citation string.
    """
    result = verify_cite(context="", citation="10.1038/171737a0")
    assert result["verified"] is False
    assert result["citation"] is None
    assert result["decision"] is None
    assert "error" in result
    assert "context" in result["error"].lower() or "claim" in result["error"].lower()


def test_verify_cite_blank_context_also_produces_error_not_silent_fallback():
    result = verify_cite(context="   ", citation="AI tutoring improves critical thinking")
    assert result["verified"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# DOI-only identity shortcut -- exact match passes G6 immediately without
# needing title-token overlap; a non-matching DOI correctly still fails.
# ---------------------------------------------------------------------------

_WATSON_CRICK_DOI = "10.1038/171737a0"

_WATSON_CRICK_WORK = _openalex_work(
    work_id="https://openalex.org/W2013407343",
    # Deliberately a title/abstract sharing ZERO meaningful tokens with the
    # bare DOI query string below -- this isolates the exact-identifier
    # shortcut from the fuzzy title matcher entirely.
    title="Molecular structure of nucleic acids: a structure for deoxyribose nucleic acid",
    authors=["J. D. Watson", "F. H. C. Crick"],
    year=1953,
    doi=_WATSON_CRICK_DOI,
    abstract="A structure for deoxyribose nucleic acid has been proposed",
)


def test_doi_only_citation_exact_match_passes_g6_immediately_without_title_overlap():
    work = CanonicalWork(candidates=[_candidate(_WATSON_CRICK_WORK)])
    passed, debug = gate_g6_identity_match(work, _WATSON_CRICK_DOI)
    assert passed is True
    assert debug["reason"] == "exact_identifier_match"
    assert debug["matched_field"] == "doi"


def test_doi_only_citation_exact_match_verifies_end_to_end():
    """Full `verify()` run: a bare-DOI query against a candidate whose `doi`
    field matches exactly must reach VERIFIED via the identifier shortcut,
    with G6_identity_match True, even though the query shares no title
    vocabulary with the candidate at all."""
    work = CanonicalWork(candidates=[_candidate(_WATSON_CRICK_WORK)])
    work, _ = verify(work, context="", query=_WATSON_CRICK_DOI)
    assert work.gate_results["G6_identity_match"] is True
    assert work.g6_identity_debug["identity"]["reason"] == "exact_identifier_match"
    assert work.state == VerificationState.VERIFIED


def test_doi_that_matches_no_candidate_falls_through_and_fails():
    """A DOI that does not match this candidate's own identifier fields, and
    shares no meaningful title vocabulary with it either, must correctly
    fall through to the fuzzy title/author matcher and FAIL there -- it
    must never be treated as a match just because it superficially looks
    like a DOI-shaped query string."""
    non_matching_doi = "10.9999/completely-unrelated-doi-12345"
    work = CanonicalWork(candidates=[_candidate(_WATSON_CRICK_WORK)])
    passed, debug = gate_g6_identity_match(work, non_matching_doi)
    assert passed is False
    assert debug.get("reason") != "exact_identifier_match"


def test_doi_that_matches_no_candidate_rejects_end_to_end():
    non_matching_doi = "10.9999/completely-unrelated-doi-12345"
    work = CanonicalWork(candidates=[_candidate(_WATSON_CRICK_WORK)])
    work, _ = verify(work, context="", query=non_matching_doi)
    assert work.gate_results["G6_identity_match"] is False
    assert work.state == VerificationState.REJECTED
    decision, debug = gate_admission_decision(work, relation="UNCLEAR")
    assert decision == Decision.REJECT
    assert "G6_identity_match_failed" in debug["reason"]


# ===========================================================================
# TASK 2 -- evidence-status regression (exact repro cases from the external
# report -- OBJECTIVE-only and HYPOTHESIS-only passages must never ADMIT)
# ===========================================================================

_SOCIAL_MEDIA_CLAIM = "social media causes depression among adolescents"
_SOCIAL_MEDIA_OBJECTIVE_EVIDENCE = (
    "This study examined whether social media causes depression among "
    "adolescents."
)

_AI_TUTORING_CLAIM = "AI tutoring improves critical thinking in students"
_AI_TUTORING_HYPOTHESIS_EVIDENCE = (
    "We discuss the hypothesis that AI tutoring improves critical thinking "
    "in students."
)


def test_social_media_objective_only_passage_classifies_objective():
    assert classify_statement_type(_SOCIAL_MEDIA_OBJECTIVE_EVIDENCE) == "OBJECTIVE"


def test_ai_tutoring_hypothesis_only_passage_classifies_hypothesis():
    assert classify_statement_type(_AI_TUTORING_HYPOTHESIS_EVIDENCE) == "HYPOTHESIS"


def test_social_media_objective_evidence_would_look_like_supports_via_relation_alone():
    """Sanity check on the bug this layer fixes: `classify_relation()` alone
    (no statement-type layer) really does call this SUPPORTS -- high shared
    vocabulary, no negation/reversal signal. The statement-type layer is
    what has to catch it, not the relation classifier."""
    assert classify_relation(_SOCIAL_MEDIA_OBJECTIVE_EVIDENCE, _SOCIAL_MEDIA_CLAIM) == "SUPPORTS"


def test_ai_tutoring_hypothesis_evidence_would_look_like_supports_via_relation_alone():
    assert classify_relation(_AI_TUTORING_HYPOTHESIS_EVIDENCE, _AI_TUTORING_CLAIM) == "SUPPORTS"


def _build_work(title: str, doi: str, abstract: str) -> CanonicalWork:
    work_json = _openalex_work(
        work_id=f"https://openalex.org/{doi}",
        title=title,
        authors=["A. Researcher"],
        year=2023,
        doi=doi,
        abstract=abstract,
    )
    return CanonicalWork(candidates=[_candidate(work_json)])


def test_gate_admission_decision_holds_objective_only_evidence_despite_supports_relation():
    work = _build_work(
        title="Social media use and depression among adolescents",
        doi="10.1/social-media-objective",
        abstract=_SOCIAL_MEDIA_OBJECTIVE_EVIDENCE,
    )
    work, _ = verify(work, context="", query="social media causes depression among adolescents")
    assert work.state == VerificationState.VERIFIED
    relation = classify_relation(_SOCIAL_MEDIA_OBJECTIVE_EVIDENCE, _SOCIAL_MEDIA_CLAIM)
    statement_type = classify_statement_type(_SOCIAL_MEDIA_OBJECTIVE_EVIDENCE)
    decision, debug = gate_admission_decision(
        work, relation, statement_type=statement_type
    )
    assert decision == Decision.HOLD
    assert decision != Decision.ADMIT
    assert "OBJECTIVE" in debug["reason"]


def test_gate_admission_decision_holds_hypothesis_only_evidence_despite_supports_relation():
    work = _build_work(
        title="AI tutoring and critical thinking in students",
        doi="10.1/ai-tutoring-hypothesis",
        abstract=_AI_TUTORING_HYPOTHESIS_EVIDENCE,
    )
    work, _ = verify(work, context="", query="AI tutoring improves critical thinking in students")
    assert work.state == VerificationState.VERIFIED
    relation = classify_relation(_AI_TUTORING_HYPOTHESIS_EVIDENCE, _AI_TUTORING_CLAIM)
    statement_type = classify_statement_type(_AI_TUTORING_HYPOTHESIS_EVIDENCE)
    decision, debug = gate_admission_decision(
        work, relation, statement_type=statement_type
    )
    assert decision == Decision.HOLD
    assert decision != Decision.ADMIT
    assert "HYPOTHESIS" in debug["reason"]


def test_resolve_citations_social_media_objective_case_holds_not_admits_end_to_end():
    """Full pipeline (`resolve_citations()`, matching
    `tests/test_cite_use_gate.py`'s technique): the OBJECTIVE-only passage
    must land in `held`, never `verified`."""
    work_json = _openalex_work(
        work_id="https://openalex.org/W-social-media-objective",
        title="Social media use and depression among adolescents",
        authors=["A. Researcher"],
        year=2023,
        doi="10.1/social-media-objective-e2e",
        abstract=_SOCIAL_MEDIA_OBJECTIVE_EVIDENCE,
    )
    adapter = _StubAdapter([work_json])
    result = resolve_citations(
        context=_SOCIAL_MEDIA_CLAIM,
        queries=["social media causes depression among adolescents"],
        adapters=[adapter],
    )
    assert result["verified"] == []
    cite_use = result["cite_uses"][0]
    assert cite_use.decision == Decision.HOLD
    assert len(result["held"]) == 1


def test_resolve_citations_ai_tutoring_hypothesis_case_holds_not_admits_end_to_end():
    work_json = _openalex_work(
        work_id="https://openalex.org/W-ai-tutoring-hypothesis",
        title="AI tutoring and critical thinking in students",
        authors=["A. Researcher"],
        year=2023,
        doi="10.1/ai-tutoring-hypothesis-e2e",
        abstract=_AI_TUTORING_HYPOTHESIS_EVIDENCE,
    )
    adapter = _StubAdapter([work_json])
    result = resolve_citations(
        context=_AI_TUTORING_CLAIM,
        queries=["AI tutoring improves critical thinking in students"],
        adapters=[adapter],
    )
    assert result["verified"] == []
    cite_use = result["cite_uses"][0]
    assert cite_use.decision == Decision.HOLD
    assert len(result["held"]) == 1


# ===========================================================================
# TASK 3 -- proposition/relation regression (open word-class gaps, and a
# genuine QUALIFIES case)
# ===========================================================================


def test_reduces_increases_not_in_closed_pair_dictionaries():
    """Sanity check on the fixture: confirms `reduces`/`increases` (as a
    PAIR) is genuinely absent from the closed pair-list mechanism, so a
    CHALLENGES verdict below is attributable to the round-4 open word-class
    signal, not to a pair-list entry that happened to exist all along."""
    from thaicite.evidence.relation import _DIRECTIONAL_PAIRS, _TIGHT_DIRECTIONAL_PAIRS

    reduces_increases = (("reduces",), ("increases",))
    increases_reduces = (("increases",), ("reduces",))
    assert reduces_increases not in _DIRECTIONAL_PAIRS
    assert increases_reduces not in _DIRECTIONAL_PAIRS
    assert reduces_increases not in _TIGHT_DIRECTIONAL_PAIRS
    assert increases_reduces not in _TIGHT_DIRECTIONAL_PAIRS


def test_protects_damages_not_in_closed_pair_dictionaries():
    from thaicite.evidence.relation import _DIRECTIONAL_PAIRS, _TIGHT_DIRECTIONAL_PAIRS

    protects_damages = (("protects",), ("damages",))
    damages_protects = (("damages",), ("protects",))
    assert protects_damages not in _DIRECTIONAL_PAIRS
    assert damages_protects not in _DIRECTIONAL_PAIRS
    assert protects_damages not in _TIGHT_DIRECTIONAL_PAIRS
    assert damages_protects not in _TIGHT_DIRECTIONAL_PAIRS


def test_reduces_vs_increases_flags_polarity_mismatch_never_supports():
    claim = "This intervention increases hospital readmission rates for discharged patients."
    passage = (
        "A five-year cohort study found the intervention reduces hospital "
        "readmission rates for discharged patients in practice."
    )
    result = classify_relation(passage, claim)
    assert result != "SUPPORTS", result
    assert result == "CHALLENGES", result


def test_protects_vs_damages_flags_polarity_mismatch_never_supports():
    claim = "The new coating protects the underlying metal surface from corrosion."
    passage = (
        "Laboratory testing found the new coating damages the underlying "
        "metal surface from corrosion in humid conditions."
    )
    result = classify_relation(passage, claim)
    assert result != "SUPPORTS", result
    assert result == "CHALLENGES", result


def test_reduces_increases_admission_decision_never_admits():
    """End-to-end through `gate_admission_decision()`: a REAL directional
    mismatch (CHALLENGES) against an intended SUPPORTS use must never
    ADMIT."""
    claim = "This intervention increases hospital readmission rates for discharged patients."
    passage = (
        "The trial found the intervention reduces hospital "
        "readmission rates for discharged patients in practice."
    )
    work = _build_work(
        title="Hospital readmission rates for discharged patients after intervention",
        doi="10.1/reduces-increases",
        abstract=passage,
    )
    work, _ = verify(
        work,
        context="",
        query="intervention hospital readmission rates for discharged patients",
    )
    assert work.state == VerificationState.VERIFIED
    relation = classify_relation(passage, claim)
    statement_type = classify_statement_type(passage)
    decision, _debug = gate_admission_decision(
        work, relation, statement_type=statement_type
    )
    assert decision != Decision.ADMIT
    assert decision == Decision.REJECT


# ---------------------------------------------------------------------------
# QUALIFIES: narrower-scope-than-claimed evidence -> HOLD, never ADMIT.
# ---------------------------------------------------------------------------


def test_qualifies_narrower_scope_evidence_maps_to_qualifies_relation():
    claim = "The training program improves job placement outcomes for participants."
    passage = (
        "The trial found the training program improves job placement "
        "outcomes only among adults under 30."
    )
    assert classify_relation(passage, claim) == "QUALIFIES"


def test_qualifies_case_holds_not_admits_via_admission_decision():
    claim = "The training program improves job placement outcomes for participants."
    passage = (
        "The trial found the training program improves job placement "
        "outcomes only among adults under 30."
    )
    work = _build_work(
        title="Job placement outcomes for training program participants",
        doi="10.1/qualifies-case",
        abstract=passage,
    )
    work, _ = verify(
        work, context="", query="training program job placement outcomes participants"
    )
    assert work.state == VerificationState.VERIFIED
    relation = classify_relation(passage, claim)
    statement_type = classify_statement_type(passage)
    decision, debug = gate_admission_decision(work, relation, statement_type=statement_type)
    assert decision == Decision.HOLD
    assert decision != Decision.ADMIT
    assert "QUALIFIES" in debug["reason"]


def test_qualifies_case_holds_end_to_end_via_resolve_citations():
    claim = "The training program improves job placement outcomes for participants."
    passage = (
        "The trial found the training program improves job placement "
        "outcomes only among adults under 30."
    )
    work_json = _openalex_work(
        work_id="https://openalex.org/W-qualifies-case",
        title="Job placement outcomes for training program participants",
        authors=["A. Researcher"],
        year=2023,
        doi="10.1/qualifies-case-e2e",
        abstract=passage,
    )
    adapter = _StubAdapter([work_json])
    result = resolve_citations(
        context=claim,
        queries=["training program job placement outcomes participants"],
        adapters=[adapter],
    )
    assert result["verified"] == []
    cite_use = result["cite_uses"][0]
    assert cite_use.relation == "QUALIFIES"
    assert cite_use.decision == Decision.HOLD
    assert len(result["held"]) == 1


# ===========================================================================
# TASK 4 -- coverage-truth regression (real ThaiIndex/ThaiJOAdapter, real
# router + engine pipeline, no network)
# ===========================================================================


@pytest.fixture()
def _thai_index():
    with tempfile.TemporaryDirectory() as tmpdir:
        idx = ThaiIndex(Path(tmpdir) / "index.db")
        yield idx
        idx.close()


def _upsert(index: ThaiIndex, *, endpoint: str, native_id: str, title: str, harvested_at: float) -> None:
    index.upsert_record(
        endpoint=endpoint,
        native_id=native_id,
        title=title,
        abstract="",
        raw_metadata={
            "identifier": native_id,
            "titles": [title],
            "creators": [],
            "dates": [],
            "identifiers": [],
            "descriptions": [],
        },
        harvested_at=harvested_at,
    )


_THAIJO_ENDPOINT = "https://sc01.tci-thaijo.org/index.php/index/oai"
_QUERY_WITH_NO_MATCH_IN_SNAPSHOT = "หัวข้อที่ไม่มีอยู่จริงแน่นอนเลยในระบบนี้"


def test_stale_thaijo_index_zero_matches_reports_stale_not_ok_full_pipeline(_thai_index):
    """A synthetic ThaiJO index that genuinely HAS records, but whose
    recorded sync timestamp is manually aged past `DEFAULT_STALE_THRESHOLD_S`,
    and which returns zero matches for the query -- driven through the REAL
    `route()` -> `discover_citations()` -> `update_coverage()` pipeline
    (not a hand-built fake engine-result dict) -- must report STALE, never
    OK/SEARCHED_OK."""
    old_sync = time.time() - (DEFAULT_STALE_THRESHOLD_S + 3600)
    _upsert(
        _thai_index,
        endpoint=_THAIJO_ENDPOINT,
        native_id="oai:tci-thaijo.org:article/stale-1",
        title="Unrelated botany survey of northern Thailand",
        harvested_at=old_sync,
    )
    _thai_index.save_endpoint_status(
        code="sc01", url=_THAIJO_ENDPOINT, status="OK", records_harvested=1, synced_at=old_sync
    )

    adapter = ThaiJOAdapter(index=_thai_index)
    decision = route(_QUERY_WITH_NO_MATCH_IN_SNAPSHOT, _QUERY_WITH_NO_MATCH_IN_SNAPSHOT, [adapter])

    result = discover_citations(context=_QUERY_WITH_NO_MATCH_IN_SNAPSHOT, adapters=decision.adapters)
    decision.update_coverage(result)

    assert result["candidates"] == []
    by_source = {e.source: e for e in decision.coverage}
    assert by_source["THAIJO"].status == cov.STALE
    assert by_source["THAIJO"].status != "OK"
    assert "stale" in (by_source["THAIJO"].reason or "").lower()
    assert cov.coverage_has_stale(decision.coverage) is True


def test_freshly_synced_thaijo_index_zero_matches_reports_searched_ok_full_pipeline(_thai_index):
    """Same shape as the STALE case above, but the recorded sync timestamp
    is recent (well within `DEFAULT_STALE_THRESHOLD_S`) -- a genuinely fresh
    'searched, nothing matched' outcome must report SEARCHED_OK, never
    STALE and never left as PLANNED."""
    fresh_sync = time.time() - 60
    _upsert(
        _thai_index,
        endpoint=_THAIJO_ENDPOINT,
        native_id="oai:tci-thaijo.org:article/fresh-1",
        title="Unrelated botany survey of northern Thailand",
        harvested_at=fresh_sync,
    )
    _thai_index.save_endpoint_status(
        code="sc01", url=_THAIJO_ENDPOINT, status="OK", records_harvested=1, synced_at=fresh_sync
    )

    adapter = ThaiJOAdapter(index=_thai_index)
    decision = route(_QUERY_WITH_NO_MATCH_IN_SNAPSHOT, _QUERY_WITH_NO_MATCH_IN_SNAPSHOT, [adapter])

    result = discover_citations(context=_QUERY_WITH_NO_MATCH_IN_SNAPSHOT, adapters=decision.adapters)
    decision.update_coverage(result)

    assert result["candidates"] == []
    by_source = {e.source: e for e in decision.coverage}
    assert by_source["THAIJO"].status == cov.SEARCHED_OK
    assert by_source["THAIJO"].status != cov.STALE
    assert by_source["THAIJO"].status != cov.PLANNED
    assert cov.coverage_has_stale(decision.coverage) is False


def test_routed_adapter_never_actually_queried_stays_planned_not_ok():
    """A caller that only builds `RouteDecision.coverage` (via `route()`)
    but whose subsequent engine call never actually searches an adapter
    (empty query family, e.g. an empty/whitespace context with no
    `extra_queries`) must leave that adapter's coverage row `PLANNED` --
    never silently promoted to any confirmed status just because
    `update_coverage()` ran."""
    adapters = [OpenAlexAdapter(), ThaiJOAdapter()]
    decision = route("", "", adapters)

    # discover_citations() with an empty context and no extra_queries builds
    # an empty query family (see core/engine.py::discover_citations's own
    # docstring/fallback) -- no adapter is ever actually invoked this call.
    result = discover_citations(context="", adapters=decision.adapters)
    assert result["candidates"] == []

    decision.update_coverage(result)
    by_source = {e.source: e for e in decision.coverage}
    assert by_source["OPENALEX"].status == cov.PLANNED
    assert by_source["OPENALEX"].status != cov.SEARCHED_OK
    assert cov.coverage_has_unconfirmed_planned(decision.coverage) is True
