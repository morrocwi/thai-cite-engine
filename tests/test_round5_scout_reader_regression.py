"""Round 5 regression suite -- the AI Scout/Reader MCP redesign
(2026-09-20).

Read `core/source_resolution.py`, `core/evidence_fetch.py`,
`evidence/verifier.py::check_claim_evidence()`/`gate_admission_decision()`
and `mcp_server.py` before touching this file -- it exercises the CURRENT
shapes of those modules, offline only, no network, no mocking of the
functions under test beyond swapping in hand-authored stub adapters (the
same `_WorkingAdapter`/`_EmptyAdapter` pattern already used throughout this
suite, e.g. `test_source_resolution.py`, `test_evidence_fetch.py`,
`test_engine_offline.py`).

Scope of THIS file (the pieces not already covered elsewhere):

  TASK 1 (resolve_source) and TASK 2 (fetch_evidence) already have their
    own dedicated, thorough offline suites -- `test_source_resolution.py`
    and `test_evidence_fetch.py`. Not duplicated here.

  TASK 3(c) -- the pure-deterministic fallback path of
    `check_claim_evidence()` (no `ai_*` args) must produce EXACTLY the same
    decision `gate_admission_decision()` would have produced for the same
    claim/passage-derived (relation, statement_type) pair -- i.e. no
    regression for a caller that never participates in the AI-Reader role.
    `test_check_claim_evidence.py` already covers the AGREE/DISAGREE
    worked examples (a)/(b); this file adds the explicit round-4-vs-round-5
    equivalence check (c).

  TASK 4 -- backward compatibility: `find_cites()`/`verify_cite()` still
    return their existing, documented response shapes and behavior, now
    implemented via the 3 new primitives underneath
    (`resolve_source -> fetch_evidence -> check_claim_evidence`). This
    includes a genuine ADMIT-positive path end to end (not just the
    error/HOLD paths existing suites already cover in
    `test_round4_fixes_regression.py` / `test_thai_ground_truth_regression.py`
    / `test_discovery_cannot_admit_regression.py`).

  Plus the explicit, founder-mandated "no bundled LLM call" constraint --
    static, source-level confirmation that no `openai`/`anthropic` SDK is
    imported anywhere in `src/thaicite/`.
"""

from __future__ import annotations

import pathlib
import re

from thaicite.adapters.base import RawRecord
from thaicite.adapters.openalex import OpenAlexAdapter
from thaicite.core.models import (
    CanonicalWork,
    Candidate,
    Decision,
    RelationLabel,
    VerificationState,
)
from thaicite.evidence.verifier import (
    VERIFY_MODE_IDENTITY,
    check_claim_evidence,
    gate_admission_decision,
)

_ADAPTER = OpenAlexAdapter()


def _inverted_index(text: str) -> dict[str, list[int]]:
    index: dict[str, list[int]] = {}
    for pos, word in enumerate(text.split()):
        index.setdefault(word, []).append(pos)
    return index


def _openalex_work_json(*, work_id, title, authors, year, doi, abstract=""):
    return {
        "id": work_id,
        "title": title,
        "display_name": title,
        "publication_year": year,
        "doi": f"https://doi.org/{doi}" if doi else None,
        "authorships": [
            {"author": {"display_name": name}, "institutions": []} for name in authors
        ],
        "primary_location": {
            "landing_page_url": f"https://example.org/{work_id}",
            "source": {"display_name": "Example Venue", "issn": ["1234-5678"]},
        },
        "abstract_inverted_index": _inverted_index(abstract) if abstract else None,
    }


class _WorkingAdapter:
    """Same offline stub pattern as `test_source_resolution.py`/
    `test_evidence_fetch.py` -- no network call."""

    name = "OPENALEX"

    def __init__(self, work_jsons):
        self._records = [
            RawRecord(source_record_id=w["id"], raw_metadata=w) for w in work_jsons
        ]

    def search(self, query):
        return list(self._records)

    def to_candidates(self, records):
        return _ADAPTER.to_candidates(records)


# ===========================================================================
# TASK 3(c) -- pure-deterministic fallback == gate_admission_decision()
# ===========================================================================


def _verified_work() -> CanonicalWork:
    """A minimal, real `CanonicalWork` in VERIFIED state -- only `work.state`
    is read by the shared mapping this test exercises (see
    `evidence/verifier.py::_admission_from_resolved_signals`), so the
    candidate's own content is irrelevant beyond satisfying `Candidate`'s
    real-source-record construction guarantee."""
    candidate = Candidate(
        source_adapter="OPENALEX",
        source_record_id="https://openalex.org/W_FALLBACK_TEST",
        title="placeholder",
    )
    return CanonicalWork(candidates=[candidate], state=VerificationState.VERIFIED)


_FALLBACK_CASES = [
    # (claim, passage) -- a genuine RESULT+SUPPORTS pair -> ADMIT.
    (
        "AI tutoring increases student time on task",
        "The study found that AI tutoring significantly increased time on "
        "task among students.",
    ),
    # A genuine RESULT+CHALLENGES pair -> REJECT (intended_relation=SUPPORTS).
    (
        "social media use causes depression",
        "The trial found no significant association between social media "
        "use and depression.",
    ),
    # A non-finding statement type (HYPOTHESIS/OBJECTIVE-shaped) -> HOLD.
    (
        "AI tutoring improves critical thinking in students",
        "We discuss the hypothesis that AI tutoring improves critical "
        "thinking in students.",
    ),
]


def test_no_ai_fallback_matches_gate_admission_decision_exactly():
    """The most important backward-compatibility guarantee in this round:
    a caller that never supplies `ai_statement_type`/`ai_relation` (e.g. the
    CLI, or `verify_cite()`'s own internal pure-deterministic call) must get
    BYTE-IDENTICAL decisions to round 4's `gate_admission_decision()`, for
    every one of ADMIT/REJECT/HOLD -- not merely "usually agrees"."""
    for claim, passage in _FALLBACK_CASES:
        new_result = check_claim_evidence(claim=claim, passage=passage)
        assert new_result["agreement"] is None, (
            "no ai_* args supplied -- agreement must be None (pure "
            "deterministic-only path), not True/False"
        )
        assert new_result["ai_proposed"] is None

        resolved_relation = new_result["deterministic_checked"]["relation"]
        resolved_statement_type = new_result["deterministic_checked"]["statement_type"]

        old_decision, old_debug = gate_admission_decision(
            _verified_work(),
            relation=resolved_relation,
            intended_relation=RelationLabel.SUPPORTS,
            mode=VERIFY_MODE_IDENTITY,
            statement_type=resolved_statement_type,
        )

        assert new_result["decision"] == old_decision, (
            f"claim={claim!r} passage={passage!r}: round-5 fallback decision "
            f"{new_result['decision']!r} != round-4 gate_admission_decision() "
            f"{old_decision!r} (debug={old_debug!r})"
        )


def test_no_ai_fallback_covers_all_three_decisions_in_this_suite():
    """Sanity check on the fixture set itself -- the 3 cases above must
    actually exercise ADMIT, REJECT, and HOLD at least once each, or the
    equivalence test above could pass trivially on a single decision."""
    decisions = {
        check_claim_evidence(claim=claim, passage=passage)["decision"]
        for claim, passage in _FALLBACK_CASES
    }
    assert decisions == {Decision.ADMIT, Decision.REJECT, Decision.HOLD}


# ===========================================================================
# TASK 4 -- backward compatibility: find_cites() / verify_cite()
# ===========================================================================


_TUTORING_TITLE = "Effects of AI Tutoring on Student Engagement"
_TUTORING_WORK = _openalex_work_json(
    work_id="https://openalex.org/W_TUTORING_ADMIT",
    title=_TUTORING_TITLE,
    authors=["Jane Researcher"],
    year=2023,
    doi="10.1234/tutoring-admit",
    abstract=(
        "The study found that AI tutoring significantly increased time on "
        "task among students."
    ),
)


def test_verify_cite_admit_positive_path_backward_compatible_shape(monkeypatch):
    """A genuine end-to-end ADMIT through the real (unmocked) `verify_cite()`
    public function, offline, via `resolve_source -> fetch_evidence ->
    check_claim_evidence` under the hood -- confirms the documented
    pre-existing response shape (`verified`, `identity_verified`,
    `decision`, `citation`, `rejected`, `held`, `not_found`, `coverage`,
    `coverage_all_negative`, `coverage_has_stale`) is unchanged and that a
    real ADMIT still reaches `verified=True` with a populated `citation`
    dict -- the positive-path case existing suites only cover indirectly
    via HOLD/REJECT/error paths."""
    import thaicite.mcp_server as mcp_server

    adapter = _WorkingAdapter([_TUTORING_WORK])
    monkeypatch.setattr(mcp_server, "_default_adapters", lambda: [adapter])

    result = mcp_server.verify_cite(
        context="AI tutoring increases student time on task",
        citation=_TUTORING_TITLE,
    )

    assert set(result.keys()) == {
        "verified",
        "identity_verified",
        "decision",
        "citation",
        "rejected",
        "held",
        "not_found",
        "coverage",
        "coverage_all_negative",
        "coverage_has_stale",
    }
    assert result["decision"] == Decision.ADMIT
    assert result["verified"] is True
    assert result["identity_verified"] is True
    assert result["citation"] is not None
    assert result["citation"]["title"] == _TUTORING_TITLE
    assert result["citation"]["doi"] == "10.1234/tutoring-admit"
    assert result["rejected"] == {}
    assert result["held"] == {}
    assert result["not_found"] == {}


_DEPRESSION_TITLE = "Social Media Use and Adolescent Depression: A Trial"
_DEPRESSION_WORK = _openalex_work_json(
    work_id="https://openalex.org/W_DEPRESSION_CHALLENGE",
    title=_DEPRESSION_TITLE,
    authors=["Pat Researcher"],
    year=2022,
    doi="10.1234/depression-challenge",
    abstract=(
        "The trial found no significant association between social media "
        "use and depression."
    ),
)


def test_verify_cite_reject_positive_path_still_shapes_rejected_not_citation(monkeypatch):
    """Same wiring, a real directional mismatch (claim wants SUPPORTS but
    the fetched passage's own real finding CHALLENGES it) -> REJECT,
    `verified` False, `citation` None, detail under `rejected` keyed by
    `<adapter>:<record_id>` -- the pre-existing documented shape. Same
    claim/passage pair already proven (in `test_check_claim_evidence.py`
    and the fallback-equivalence test above) to classify as
    RESULT+CHALLENGES."""
    import thaicite.mcp_server as mcp_server

    adapter = _WorkingAdapter([_DEPRESSION_WORK])
    monkeypatch.setattr(mcp_server, "_default_adapters", lambda: [adapter])

    result = mcp_server.verify_cite(
        context="social media use causes depression",
        citation=_DEPRESSION_TITLE,
    )

    assert result["identity_verified"] is True
    assert result["verified"] is False
    assert result["citation"] is None
    assert result["decision"] == Decision.REJECT
    assert result["rejected"]
    label = next(iter(result["rejected"]))
    assert label == "OPENALEX:https://openalex.org/W_DEPRESSION_CHALLENGE"
    assert result["held"] == {}


def test_find_cites_backward_compatible_top_level_shape(monkeypatch):
    """`find_cites()`'s documented top-level keys are unchanged by the
    round-5 rewrite -- it still calls `core.engine.discover_citations()`
    directly (see `mcp_server.py`'s own module docstring for why), never the
    3 new primitives."""
    import thaicite.mcp_server as mcp_server

    adapter = _WorkingAdapter([_TUTORING_WORK])
    monkeypatch.setattr(mcp_server, "_default_adapters", lambda: [adapter])

    result = mcp_server.find_cites(context="AI tutoring and student engagement")

    assert set(result.keys()) == {
        "domain",
        "candidates",
        "not_found_queries",
        "rejected_count",
        "query_family",
        "coverage",
        "coverage_all_negative",
        "coverage_has_stale",
    }
    assert isinstance(result["candidates"], list)
    for candidate_dict in result["candidates"]:
        assert set(candidate_dict.keys()) == {
            "title",
            "authors",
            "year",
            "doi",
            "pmid",
            "source_adapter",
            "source_record_id",
            "url",
            "thai_relevance",
            "matched_keywords",
            "evidence_level",
        }
        assert "decision" not in candidate_dict


# ===========================================================================
# Founder constraint -- no bundled LLM SDK call anywhere in src/thaicite/.
# ===========================================================================

_SRC_ROOT = pathlib.Path(__file__).resolve().parent.parent / "src" / "thaicite"
_FORBIDDEN_IMPORT_RE = re.compile(
    r"^\s*(import|from)\s+(openai|anthropic)\b", re.MULTILINE
)


def test_no_llm_sdk_imported_anywhere_in_thaicite_source():
    """Explicit founder decision, this session: no `openai`/`anthropic` SDK
    import, no new vendor dependency, no API-key requirement, no bundled
    per-call cost anywhere in `src/thaicite/` -- the calling AI (already an
    LLM by construction of MCP) plays Scout+Reader instead. Static,
    source-level confirmation across every `.py` file in the package, not
    just the 3 new modules."""
    offenders = []
    for path in _SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _FORBIDDEN_IMPORT_RE.search(text):
            offenders.append(str(path.relative_to(_SRC_ROOT)))
    assert not offenders, f"forbidden LLM SDK import found in: {offenders}"
