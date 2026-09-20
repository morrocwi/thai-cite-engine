"""Offline tests for the discovery-vs-identity role-collision fix.

Covers, without any network call:
  - Task 1: `evidence.verifier.verify()`/`gate_admission_decision()`'s
    `mode="discovery"` path -- G6's strict candidate-vs-query identity match
    is not required to reach VERIFIED; relevance is judged by topical
    overlap (`gate_g6_discovery_relevance`) instead, and the 3-way
    ADMIT/REJECT/HOLD semantics are preserved (never a force-ADMIT).
  - Task 2: `core.engine.discover_citations()` wires in
    `routing.query_planner.plan_queries` and fuses/dedupes candidates found
    under different family members into ONE CanonicalWork/CiteUse.
  - The real Thai ground-truth worked example from the task: a genuinely
    on-topic Islamic-law-and-women work, found under a broad, unspaced Thai
    discovery context, must not be rejected purely for identity mismatch.

Reuses the same offline OpenAlex/ThaiJO-shaped fixture helpers already used
by test_cite_use_gate.py / test_engine_offline.py -- no mocking of the
functions under test themselves.
"""

from __future__ import annotations

from thaicite.adapters.base import RawRecord
from thaicite.adapters.openalex import OpenAlexAdapter
from thaicite.adapters.thaijo import ThaiJOAdapter
from thaicite.core.engine import discover_citations, resolve_citations
from thaicite.core.models import CanonicalWork, Decision
from thaicite.evidence.verifier import (
    gate_g6_discovery_relevance,
    gate_g6_identity_match,
    verify,
)

_OA = OpenAlexAdapter()
_THAIJO = ThaiJOAdapter()


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
    return _OA.to_candidates([record])[0]


# ---------------------------------------------------------- Task 1 (verify) --


def test_discovery_mode_does_not_require_g6_identity_match():
    """A real, on-topic candidate whose title shares nothing with a broad
    topic phrase must still reach VERIFIED under mode='discovery', because
    the topic phrase and the title DO share real topical vocabulary."""
    work_json = _openalex_work(
        work_id="D1",
        title="AI tutoring systems and student engagement outcomes",
        authors=["Pat Lee"],
        year=2023,
        doi="10.1/d1",
        abstract="This trial helped student focus during lessons and reduced dropout.",
    )
    candidate = _candidate(work_json)
    work = CanonicalWork(candidates=[candidate])
    # Shares only "student" (1 token) with the candidate's TITLE -- G6
    # identity must fail -- but shares "student" (title) + "focus" (abstract)
    # with the combined title+abstract text discovery relevance checks.
    context = "improving student focus using classroom technology"

    # G6 identity match against this broad phrase (never a citation string)
    # correctly fails -- this is the bug: it was wrongly used as the gate.
    identity_ok, _ = gate_g6_identity_match(work, context)
    assert identity_ok is False

    # Discovery relevance passes on the same pair instead.
    relevance_ok, _ = gate_g6_discovery_relevance(work, context)
    assert relevance_ok is True

    work, _ = verify(work, context=context, query=context, mode="discovery")
    assert work.state == "VERIFIED"
    # G6_identity_match is still recorded for transparency, but did not gate.
    assert work.gate_results["G6_identity_match"] is False
    assert work.gate_results["G6_discovery_relevance"] is True


def test_identity_mode_unchanged_default_still_requires_g6():
    """mode defaults to 'identity' -- an existing caller that never passes
    `mode` gets byte-identical behavior to before this fix."""
    work_json = _openalex_work(
        work_id="D2",
        title="Completely Unrelated Botany Survey",
        authors=["Someone Else"],
        year=2019,
        doi="10.1/d2",
        abstract="A survey of botanical species in a tropical region.",
    )
    candidate = _candidate(work_json)
    work = CanonicalWork(candidates=[candidate])
    work, _ = verify(work, context="", query="AI tutoring and student engagement outcomes")
    assert work.state == "REJECTED"
    assert work.gate_results["G6_identity_match"] is False


def test_discovery_relevance_requires_real_overlap_not_one_generic_word():
    work_json = _openalex_work(
        work_id="D3",
        title="A study of unrelated marine biology topics",
        authors=["X Y"],
        year=2021,
        doi="10.1/d3",
        abstract="This study examines marine biology in coastal regions.",
    )
    candidate = _candidate(work_json)
    work = CanonicalWork(candidates=[candidate])
    # Shares only the generic word "study" with the context below.
    ok, debug = gate_g6_discovery_relevance(work, "a study about AI tutoring")
    assert ok is False


# ------------------------------------------------------- Task 1 (decision) --


def test_discovery_mode_still_holds_when_evidence_does_not_clearly_support():
    """Lenient identity matching in discovery mode must never force-ADMIT a
    candidate whose evidence does not clearly support the claim -- it still
    lands on HOLD, the same 3-way semantics as identity mode."""
    work_json = _openalex_work(
        work_id="D4",
        title="AI tutoring systems and engagement outcomes",
        authors=["Pat Lee"],
        year=2023,
        doi="10.1/d4",
        # Zero overlap with the claim below -> classify_relation() returns
        # UNCLEAR (no shared topic terms at all), even though the TITLE
        # alone is enough for discovery relevance to pass.
        abstract="A brief note on unrelated facility maintenance schedules for staff members.",
    )

    class _StubAdapter:
        name = "OPENALEX"

        def search(self, query):
            return [
                RawRecord(source_record_id=work_json["id"], raw_metadata=work_json)
            ]

        def to_candidates(self, records):
            return _OA.to_candidates(records)

    result = discover_citations(
        context="AI tutoring systems for engagement", adapters=[_StubAdapter()]
    )
    assert result["verified"] == []
    assert len(result["held"]) == 1
    cite_use = result["cite_uses"][0]
    assert cite_use.decision == Decision.HOLD


# --------------------------------------------------------------- Task 2 --


def test_discover_citations_wires_in_query_planner_family():
    class _RecordingAdapter:
        name = "OPENALEX"

        def __init__(self):
            self.queries_seen = []

        def search(self, query):
            self.queries_seen.append(query)
            return []

        def to_candidates(self, records):
            return []

    adapter = _RecordingAdapter()
    result = discover_citations(context="social media causes depression", adapters=[adapter])

    # More than just the single raw context string was searched.
    assert len(adapter.queries_seen) >= 2
    assert "query_family" in result
    assert set(result["query_family"].keys()) == {"support", "challenge"}
    # A genuine semantic-opposite challenge variant was generated and searched.
    assert any("prevents" in q for q in adapter.queries_seen)


def test_discover_citations_fuses_dedupes_same_work_across_family():
    """The same real work, found under two different query-family variants,
    must produce exactly ONE CanonicalWork/CiteUse/Citation -- never two."""
    work_json = _openalex_work(
        work_id="D5",
        title="Social media use and depression risk in adolescents",
        authors=["Jane Doe"],
        year=2022,
        doi="10.1/d5",
        abstract="Social media use is a risk factor for depression among adolescents in this cohort study.",
    )
    record = RawRecord(source_record_id=work_json["id"], raw_metadata=work_json)

    class _AlwaysSameHitAdapter:
        name = "OPENALEX"

        def __init__(self):
            self.call_count = 0

        def search(self, query):
            self.call_count += 1
            return [record]

        def to_candidates(self, records):
            return _OA.to_candidates(records)

    adapter = _AlwaysSameHitAdapter()
    result = discover_citations(context="social media causes depression", adapters=[adapter])

    # The adapter was searched once per family member (more than once)...
    assert adapter.call_count >= 2
    # ...but the fused/deduped result has exactly one CiteUse for the work.
    assert len(result["cite_uses"]) == 1
    assert len(result["verified"]) + len(result["held"]) + len(result["rejected"]) == 1


# ------------------------------------------------------- worked example --


def test_thai_islamic_law_women_context_surfaces_real_on_topic_work():
    """The task's own real Thai ground-truth case: a broad, unspaced Thai
    discovery context should be able to surface a real, on-topic
    ThaiJO-shaped work without requiring 2+ literal shared tokens with the
    raw context string (the old, buggy identity-mode behavior)."""
    thai_work_raw_metadata = {
        "identifier": "oai:tci-thaijo.org:article/9911",
        "titles": ["สถานภาพสตรีมุสลิมภายใต้กฎหมายอิสลามในสังคมไทย"],
        "creators": ["สมหญิง มุสลิมะฮ์"],
        "dates": ["2020-01-01"],
        "identifiers": ["https://doi.org/10.5555/islamic-law-women-th"],
        "descriptions": [
            "บทความนี้ศึกษาสถานภาพและสิทธิของสตรีมุสลิมภายใต้กฎหมายอิสลามในบริบทสังคมไทย "
            "โดยเน้นประเด็นครอบครัวและมรดกตามหลักชะรีอะฮ์"
        ],
        "source": [],
    }

    class _ThaiWorkingAdapter:
        name = "THAIJO"

        def search(self, query):
            return [
                RawRecord(
                    source_record_id=thai_work_raw_metadata["identifier"],
                    raw_metadata=thai_work_raw_metadata,
                )
            ]

        def to_candidates(self, records):
            return _THAIJO.to_candidates(records)

    context = "หางานวิจัยไทยเกี่ยวกับกฎหมายอิสลามและผู้หญิง"

    old = resolve_citations(context=context, queries=[context], adapters=[_ThaiWorkingAdapter()])
    # OLD identity-mode behavior: rejected purely for G6 identity mismatch
    # (the topic phrase was never meant to be a citation string).
    old_label = "THAIJO:oai:tci-thaijo.org:article/9911"
    assert old_label in old["rejected"]
    assert "G6_identity_match" in old["rejected"][old_label]["reason"]

    new = discover_citations(context=context, adapters=[_ThaiWorkingAdapter()])
    # NEW discovery-mode behavior: no longer rejected for identity mismatch
    # -- the real work is discovered and lands in verified or held (a real
    # 3-way decision), never silently dropped as if it does not exist.
    assert any(
        cu.work.primary.source_record_id == thai_work_raw_metadata["identifier"]
        for cu in new["cite_uses"]
    )
    cite_use = next(
        cu for cu in new["cite_uses"]
        if cu.work.primary.source_record_id == thai_work_raw_metadata["identifier"]
    )
    assert cite_use.gate_results["G6_discovery_relevance"] is True
    assert cite_use.decision in (Decision.ADMIT, Decision.HOLD)
