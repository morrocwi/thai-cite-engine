"""Regression: discovery mode is STRUCTURALLY incapable of producing an
ADMIT decision.

2026-09-20 round 3 (`core/engine.py`, `core/models.py`) made
`discover_citations()` return `core.models.DiscoveredCandidate` objects,
a type with no `decision` field at all -- not `CiteUse` reused with
`decision` defaulted/filtered to non-ADMIT by convention. This file attacks
that guarantee from every angle the discovery-mode API surface actually
exposes, confirming each one is structurally impossible to coerce into
ADMIT, not merely "didn't happen in this one test run":

  1. The dataclass itself has no `decision` field (`dataclasses.fields()`),
     and constructing one WITH `decision=Decision.ADMIT` raises `TypeError`
     at the constructor boundary.
  2. `discover_citations()`'s return dict has no key that could carry a
     `Decision` (no "verified"/"cite_uses"/"held" -- only "candidates",
     which holds exactly the type checked in (1)).
  3. `discover_citations()`'s call graph never imports or calls
     `evidence.verifier.gate_admission_decision` or
     `evidence.relation.classify_relation` -- confirmed via the module's
     own bytecode-adjacent `__code__.co_names`/source-level check, not by
     re-running the pipeline and observing an absence this one time.
  4. The public MCP surface built on top of discovery mode
     (`mcp_server.find_cites`) never emits a `decision` key in any
     candidate dict it returns, end-to-end, through the real (unmocked)
     pipeline with a synthetic offline adapter.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from thaicite.adapters.base import RawRecord
from thaicite.adapters.thaijo import ThaiJOAdapter
from thaicite.core import engine as engine_module
from thaicite.core.engine import discover_citations
from thaicite.core.models import CanonicalWork, Decision, DiscoveredCandidate
from thaicite.evidence import relation as relation_module
from thaicite.evidence import verifier as verifier_module

_THAIJO = ThaiJOAdapter()


def _seed_work_raw() -> dict:
    return {
        "identifier": "oai:tci-thaijo.org:article/discovery-admit-1",
        "titles": ["งานวิจัยเรื่องกฎหมายอิสลามและสิทธิสตรีในสังคมไทย"],
        "creators": ["ผู้วิจัย ตัวอย่าง"],
        "dates": ["2021-01-01"],
        "identifiers": ["https://doi.org/10.5555/discovery-admit-1"],
        "descriptions": [
            "บทความนี้ศึกษาสิทธิสตรีภายใต้กฎหมายอิสลามในสังคมไทยอย่างละเอียด"
        ],
        "source": [],
    }


class _StaticAdapter:
    name = "THAIJO"

    def __init__(self, raw: dict) -> None:
        self._raw = raw

    def search(self, query: str):
        return [RawRecord(source_record_id=self._raw["identifier"], raw_metadata=self._raw)]

    def to_candidates(self, records):
        return _THAIJO.to_candidates(records)


# ---------------------------------------------------------------------------
# 1. Type-level: DiscoveredCandidate has no `decision` field, ever.
# ---------------------------------------------------------------------------


def test_discovered_candidate_dataclass_has_no_decision_field() -> None:
    field_names = {f.name for f in dataclasses.fields(DiscoveredCandidate)}
    assert "decision" not in field_names


def test_constructing_discovered_candidate_with_decision_kwarg_raises_typeerror() -> None:
    """Attempting the exact thing the task describes -- forcing
    decision=ADMIT through the discovery-mode type -- fails at construction
    time with a TypeError naming the bad kwarg, not silently accepted."""
    work_raw = _seed_work_raw()
    thaijo_candidate = _THAIJO.to_candidates(
        [RawRecord(source_record_id=work_raw["identifier"], raw_metadata=work_raw)]
    )[0]
    work = CanonicalWork(candidates=[thaijo_candidate], state="VERIFIED")

    with pytest.raises(TypeError) as excinfo:
        DiscoveredCandidate(
            work=work,
            context="topic",
            query="topic",
            decision=Decision.ADMIT,  # type: ignore[call-arg]
        )
    assert "decision" in str(excinfo.value)


def test_discovered_candidate_has_no_attribute_named_decision_even_via_getattr() -> None:
    work_raw = _seed_work_raw()
    thaijo_candidate = _THAIJO.to_candidates(
        [RawRecord(source_record_id=work_raw["identifier"], raw_metadata=work_raw)]
    )[0]
    work = CanonicalWork(candidates=[thaijo_candidate], state="VERIFIED")
    candidate = DiscoveredCandidate(work=work, context="topic", query="topic")
    assert not hasattr(candidate, "decision")
    assert getattr(candidate, "decision", "SENTINEL_NOT_PRESENT") == "SENTINEL_NOT_PRESENT"


# ---------------------------------------------------------------------------
# 2. Return-shape level: discover_citations()'s dict has no decision-carrying
#    key at all, running the real end-to-end pipeline.
# ---------------------------------------------------------------------------


def test_discover_citations_return_value_has_no_decision_carrying_key() -> None:
    adapter = _StaticAdapter(_seed_work_raw())
    result = discover_citations(
        context="หางานวิจัยไทยเกี่ยวกับกฎหมายอิสลามและผู้หญิง", adapters=[adapter]
    )
    assert set(result.keys()) == {"candidates", "rejected", "not_found_queries", "query_family"}
    for forbidden_key in ("verified", "cite_uses", "held", "decision"):
        assert forbidden_key not in result

    assert result["candidates"], "expected at least one surfaced candidate for this fixture"
    for candidate in result["candidates"]:
        assert not hasattr(candidate, "decision")
        assert candidate.__class__ is DiscoveredCandidate


def test_discover_citations_rejected_entries_also_carry_no_decision() -> None:
    """Even the `rejected` bucket (G1-G7/relevance failures) is a plain dict
    of debug strings, never a `Decision` value -- discovery mode's rejection
    path is identity/existence-only, distinct from the admission REJECT
    that only `resolve_citations()` can produce."""
    off_topic_raw = {
        "identifier": "oai:tci-thaijo.org:article/off-topic-1",
        "titles": ["A completely unrelated botany survey of tropical ferns"],
        "creators": ["Someone Else"],
        "dates": ["2019-01-01"],
        "identifiers": [],
        "descriptions": ["This paper is about fern taxonomy, nothing else."],
        "source": [],
    }
    adapter = _StaticAdapter(off_topic_raw)
    result = discover_citations(context="ferns and unrelated botany taxonomy survey topic", adapters=[adapter])
    # This fixture is deliberately ON-topic with its own context (shares
    # real vocabulary), so assert only on the *shape* invariant regardless
    # of whether it landed in candidates or rejected.
    for entry in result["rejected"].values():
        assert isinstance(entry, dict)
        assert "decision" not in entry
        for value in entry.values():
            assert value not in (Decision.ADMIT, Decision.REJECT, Decision.HOLD) or not isinstance(
                value, str
            ) or value not in Decision.ALL


# ---------------------------------------------------------------------------
# 3. Call-graph level: discover_citations()'s own module never references
#    the admission-decision machinery at all.
# ---------------------------------------------------------------------------


def test_discover_citations_source_never_calls_gate_admission_decision() -> None:
    """Static, BYTECODE-level confirmation (not "didn't happen this run",
    and not fooled by the function's own docstring mentioning these names
    in prose): `co_names` lists every global/attribute name the compiled
    function body actually references when executed -- `gate_admission_
    decision`/`classify_relation`/`CiteUse` must not appear there, the ONLY
    way an ADMIT could be produced downstream of this function."""
    code_names = engine_module.discover_citations.__code__.co_names
    assert "gate_admission_decision" not in code_names
    assert "classify_relation" not in code_names
    assert "CiteUse" not in code_names


def test_discover_citations_helper_never_calls_gate_admission_decision() -> None:
    """Same bytecode-level check on `_evaluate_discovered_work`, the ONLY
    function `discover_citations()` uses to turn a `CanonicalWork` into a
    result entry (see core/engine.py's own docstring for this claim)."""
    code_names = engine_module._evaluate_discovered_work.__code__.co_names
    assert "gate_admission_decision" not in code_names
    assert "classify_relation" not in code_names
    assert "CiteUse" not in code_names
    assert "DiscoveredCandidate" in code_names
    source = inspect.getsource(engine_module._evaluate_discovered_work)
    assert "DiscoveredCandidate(" in source


def test_gate_admission_decision_and_classify_relation_are_real_functions_not_renamed_away() -> None:
    """Guards the two checks above against a false pass: confirms the two
    forbidden names really do still exist as real, callable functions in
    their home modules (so "never appears in discover_citations' source"
    is a meaningful absence, not a trivial truth because the functions
    themselves were renamed/removed)."""
    assert callable(verifier_module.gate_admission_decision)
    assert callable(relation_module.classify_relation)


# ---------------------------------------------------------------------------
# 4. Public MCP surface: find_cites() never emits a "decision" key anywhere,
#    through the real, unmocked discovery pipeline.
# ---------------------------------------------------------------------------


def test_find_cites_public_surface_never_emits_a_decision_key(monkeypatch) -> None:
    import thaicite.mcp_server as mcp_server

    adapter = _StaticAdapter(_seed_work_raw())
    monkeypatch.setattr(mcp_server, "_default_adapters", lambda: [adapter])

    result = mcp_server.find_cites(context="หางานวิจัยไทยเกี่ยวกับกฎหมายอิสลามและผู้หญิง")

    assert "decision" not in result
    assert result["candidates"], "expected at least one candidate for this fixture"
    for candidate_dict in result["candidates"]:
        assert "decision" not in candidate_dict
