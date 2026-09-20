"""Unit tests for the Citation-Use / 3-way gate refactor (2026-09-20).

Covers, without any network call:
  - `classify_relation` (evidence/relation.py, Task 3): deterministic
    SUPPORTS/CHALLENGES/CONTEXT_ONLY/UNCLEAR baseline.
  - `gate_admission_decision` (evidence/verifier.py, Task 2): the 3-way
    ADMIT/REJECT/HOLD mapping on top of the existing G1-G7 state machine.
  - `ContextContract`/`freeze_context` (core/models.py, Task 4).
  - `resolve_citations()` still returns a `cite_uses` list and its
    pre-existing `verified`/`rejected`/`not_found_queries` keys are
    unaffected by the refactor (Task 1).

Reuses the same offline OpenAlex-shaped fixture helpers as
`test_g6_fix_offline.py` so nothing here touches a live adapter.
"""

from __future__ import annotations

from thaicite.adapters.base import RawRecord
from thaicite.adapters.openalex import OpenAlexAdapter
from thaicite.core.engine import resolve_citations
from thaicite.core.models import CanonicalWork, ContextContract, Decision, RelationLabel, freeze_context
from thaicite.evidence.relation import classify_relation
from thaicite.evidence.verifier import gate_admission_decision, verify

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


# ---------------------------------------------------------------- Task 3 --


def test_classify_relation_supports_on_clear_shared_terms_no_negation():
    passage = "AI tutoring significantly increased students time on task in the classroom study."
    claim = "AI increases students time on task"
    assert classify_relation(passage, claim) == "SUPPORTS"


def test_classify_relation_challenges_on_negation_near_shared_terms():
    passage = "The trial found no significant association between social media use and depression."
    claim = "social media use causes depression"
    assert classify_relation(passage, claim) == "CHALLENGES"


def test_classify_relation_unclear_on_thin_passage():
    assert classify_relation("ok", "AI improves critical thinking") == "UNCLEAR"


def test_classify_relation_unclear_on_empty_passage():
    assert classify_relation("", "AI improves critical thinking") == "UNCLEAR"


def test_classify_relation_unclear_on_zero_topical_overlap():
    passage = "The weather in Bangkok is hot and humid during April every year without fail."
    claim = "AI improves critical thinking in university students"
    assert classify_relation(passage, claim) == "UNCLEAR"


def test_classify_relation_context_only_on_loose_single_term_overlap():
    passage = "Students reported high satisfaction with the new campus library renovation project."
    claim = "students engagement with AI tutoring tools"
    result = classify_relation(passage, claim)
    assert result in ("CONTEXT_ONLY", "UNCLEAR")


# ---------------------------------------------------------------- Task 2 --


def _verified_work(title="AI tutoring and time on task", query="AI tutoring time on task study"):
    work_json = _openalex_work(
        work_id="W1",
        title=title,
        authors=["Jane Smith"],
        year=2024,
        doi="10.1/abc",
        abstract="AI tutoring significantly increased time on task among students in this trial.",
    )
    candidate = _candidate(work_json)
    work = CanonicalWork(candidates=[candidate])
    work, _ = verify(work, context="", query=query)
    return work


def test_gate_admission_admit_on_verified_matching_relation():
    work = _verified_work()
    decision, debug = gate_admission_decision(work, RelationLabel.SUPPORTS, intended_relation=RelationLabel.SUPPORTS)
    assert decision == Decision.ADMIT
    assert debug["work_state"] == "VERIFIED"


def test_gate_admission_reject_on_directional_mismatch():
    work = _verified_work()
    decision, debug = gate_admission_decision(work, RelationLabel.CHALLENGES, intended_relation=RelationLabel.SUPPORTS)
    assert decision == Decision.REJECT


def test_gate_admission_hold_on_unclear_relation():
    work = _verified_work()
    decision, _ = gate_admission_decision(work, RelationLabel.UNCLEAR, intended_relation=RelationLabel.SUPPORTS)
    assert decision == Decision.HOLD


def test_gate_admission_hold_on_conflict_state():
    work_json = _openalex_work(
        work_id="W2", title="Some Title", authors=["A"], year=2020, doi="10.1/x", abstract="x" * 20
    )
    candidate = _candidate(work_json)
    work = CanonicalWork(candidates=[candidate])
    from thaicite.core.models import VerificationState

    work.state = VerificationState.CONFLICT
    decision, debug = gate_admission_decision(work, RelationLabel.SUPPORTS)
    assert decision == Decision.HOLD
    assert debug["reason"] == "unresolved_metadata_conflict"


def test_gate_admission_reject_on_g6_identity_failure():
    # A candidate whose title/authors share nothing with the query -- G6
    # must fail, and the gate must map that to REJECT (a real mismatch),
    # never HOLD or ADMIT.
    work_json = _openalex_work(
        work_id="W3",
        title="Completely Unrelated Botany Survey",
        authors=["Someone Else"],
        year=2019,
        doi="10.1/y",
        abstract="A survey of botanical species in a tropical region.",
    )
    candidate = _candidate(work_json)
    work = CanonicalWork(candidates=[candidate])
    work, _ = verify(work, context="", query="AI tutoring and student time on task outcomes")
    assert work.gate_results["G6_identity_match"] is False
    decision, debug = gate_admission_decision(work, RelationLabel.UNCLEAR)
    assert decision == Decision.REJECT
    assert "G6_identity_match_failed" in debug["reason"]


# ---------------------------------------------------------------- Task 4 --


def test_freeze_context_builds_contract():
    contract = freeze_context(claim="AI improves critical thinking", intended_relation=RelationLabel.SUPPORTS)
    assert isinstance(contract, ContextContract)
    assert contract.created_before_search is True
    assert contract.claim == "AI improves critical thinking"


def test_context_contract_is_frozen():
    import dataclasses

    contract = freeze_context(claim="x")
    try:
        contract.claim = "y"  # type: ignore[misc]
        assert False, "ContextContract must be frozen"
    except dataclasses.FrozenInstanceError:
        pass


# ---------------------------------------------------------------- Task 1 --


class _StubAdapter:
    name = "stub"

    def __init__(self, records):
        self._records = records

    def search(self, query):
        return self._records

    def to_candidates(self, result):
        return [_candidate(r) for r in result]


def test_resolve_citations_returns_cite_uses_and_keeps_existing_keys():
    work_json = _openalex_work(
        work_id="W4",
        title="AI tutoring and student engagement",
        authors=["Pat Lee"],
        year=2023,
        doi="10.1/z",
        abstract="AI tutoring significantly increased engagement and time on task for students.",
    )
    adapter = _StubAdapter([work_json])
    result = resolve_citations(
        context="AI tutoring effects",
        queries=["AI tutoring student engagement time on task"],
        adapters=[adapter],
    )
    assert set(result.keys()) == {"verified", "rejected", "not_found_queries", "cite_uses"}
    assert len(result["cite_uses"]) == 1
    cite_use = result["cite_uses"][0]
    assert cite_use.decision in Decision.ALL
    assert cite_use.relation in RelationLabel.ALL


def test_resolve_citations_same_work_different_claims_can_diverge():
    work_json = _openalex_work(
        work_id="W5",
        title="AI tutoring and student time on task",
        authors=["Pat Lee"],
        year=2023,
        doi="10.1/w5",
        abstract="This trial found no significant increase in time on task from AI tutoring.",
    )
    adapter = _StubAdapter([work_json])

    support_contract = freeze_context(
        claim="AI tutoring and student time on task",
        intended_relation=RelationLabel.SUPPORTS,
    )
    challenge_contract = freeze_context(
        claim="AI tutoring and student time on task",
        intended_relation=RelationLabel.CHALLENGES,
    )

    support_result = resolve_citations(
        context="",
        queries=["AI tutoring and student time on task"],
        adapters=[adapter],
        context_contract=support_contract,
    )
    challenge_result = resolve_citations(
        context="",
        queries=["AI tutoring and student time on task"],
        adapters=[adapter],
        context_contract=challenge_contract,
    )

    support_use = support_result["cite_uses"][0]
    challenge_use = challenge_result["cite_uses"][0]
    assert support_use.relation == challenge_use.relation == "CHALLENGES"
    # Same work, same evidence, opposite intended use -> opposite decision.
    assert support_use.decision == Decision.REJECT
    assert challenge_use.decision == Decision.ADMIT
