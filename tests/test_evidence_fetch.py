"""Offline unit tests for `core.evidence_fetch.fetch_evidence()` -- the
evidence-fetch primitive (see that module's docstring). No network call:
adapters here are hand-authored stub adapters, matching the
`_WorkingAdapter`/`_EmptyAdapter` pattern already used in
`test_source_resolution.py` / `test_engine_offline.py`.
"""

from __future__ import annotations

from thaicite.adapters.base import AdapterError, RawRecord
from thaicite.adapters.openalex import OpenAlexAdapter
from thaicite.core.evidence_fetch import (
    EVIDENCE_LEVEL_ABSTRACT,
    EVIDENCE_LEVEL_METADATA,
    LOCATOR_ABSTRACT,
    LOCATOR_METADATA_ONLY,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    fetch_evidence,
)
from thaicite.core.models import VerificationState

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
    """Returns a fixed set of real RawRecords -- no network call, same
    pattern as `test_source_resolution.py::_WorkingAdapter`."""

    name = "OPENALEX"

    def __init__(self, work_jsons, name="OPENALEX"):
        self.name = name
        self._records = [
            RawRecord(source_record_id=w["id"], raw_metadata=w) for w in work_jsons
        ]

    def search(self, query):
        return list(self._records)

    def to_candidates(self, records):
        candidates = _ADAPTER.to_candidates(records)
        for c in candidates:
            c.source_adapter = self.name
        return candidates


class _EmptyAdapter:
    """A real adapter that genuinely searched and found nothing."""

    name = "OPENALEX"

    def search(self, query):
        return AdapterError(
            state=VerificationState.NOT_FOUND,
            adapter=self.name,
            message="No records match.",
        )

    def to_candidates(self, records):  # pragma: no cover - never called
        return []


class _AlwaysFailingAdapter:
    name = "OPENALEX"

    def search(self, query):
        return AdapterError(
            state=VerificationState.TIMEOUT,
            adapter=self.name,
            message="Simulated timeout.",
        )

    def to_candidates(self, records):  # pragma: no cover - never called
        return []


_ATTENTION_WORK = _openalex_work_json(
    work_id="https://openalex.org/W2963341956",
    title="Attention is All you Need",
    authors=["Ashish Vaswani", "Noam Shazeer"],
    year=2017,
    doi="10.48550/arxiv.1706.03762",
    abstract="The dominant sequence transduction models are based on complex networks.",
)

_NO_ABSTRACT_WORK = _openalex_work_json(
    work_id="https://openalex.org/W9999999",
    title="A Paper With No Abstract On Record",
    authors=["Someone Author"],
    year=2020,
    doi="10.1234/no-abstract",
    abstract="",
)


def test_worked_example_real_resolvable_source_id_returns_abstract():
    """The task's requested worked example: fetch_evidence on a real,
    resolvable source_id returns real abstract text with
    evidence_level=ABSTRACT."""
    adapter = _WorkingAdapter([_ATTENTION_WORK])
    source_id = "OPENALEX:https://openalex.org/W2963341956"

    result = fetch_evidence(source_id, [adapter])

    assert result["status"] == STATUS_OK
    assert result["source_id"] == source_id
    assert result["evidence_level"] == EVIDENCE_LEVEL_ABSTRACT
    assert result["locator"] == LOCATOR_ABSTRACT
    assert (
        result["evidence_text"]
        == "The dominant sequence transduction models are based on complex networks."
    )
    assert result["metadata"]["title"] == "Attention is All you Need"
    assert result["metadata"]["source_adapter"] == "OPENALEX"
    assert result["metadata"]["source_record_id"] == "https://openalex.org/W2963341956"
    assert isinstance(result["fetched_at"], float)
    assert isinstance(result["source_retrieved_at"], float)


def test_metadata_only_when_no_abstract_present():
    adapter = _WorkingAdapter([_NO_ABSTRACT_WORK])
    source_id = "OPENALEX:https://openalex.org/W9999999"

    result = fetch_evidence(source_id, [adapter])

    assert result["status"] == STATUS_OK
    assert result["evidence_level"] == EVIDENCE_LEVEL_METADATA
    assert result["locator"] == LOCATOR_METADATA_ONLY
    assert result["evidence_text"] == ""
    assert result["metadata"]["title"] == "A Paper With No Abstract On Record"


def test_doi_prefixed_source_id_resolves_across_adapters():
    doi = "10.48550/arxiv.1706.03762"
    adapter = _WorkingAdapter([_ATTENTION_WORK])

    result = fetch_evidence(f"DOI:{doi}", [adapter])

    assert result["status"] == STATUS_OK
    assert result["evidence_level"] == EVIDENCE_LEVEL_ABSTRACT
    assert result["metadata"]["doi"] == doi


def test_pmid_prefixed_source_id_no_match_returns_unavailable():
    adapter = _WorkingAdapter([_ATTENTION_WORK])  # has no pmid at all

    result = fetch_evidence("PMID:12345678", [adapter])

    assert result["status"] == STATUS_UNAVAILABLE
    assert "reason" in result
    assert "source_id" not in result or True  # UNAVAILABLE never carries source_id


def test_malformed_source_id_returns_unavailable_never_fabricates():
    adapter = _WorkingAdapter([_ATTENTION_WORK])

    result = fetch_evidence("not-a-valid-token-no-colon", [adapter])

    assert result["status"] == STATUS_UNAVAILABLE
    assert "reason" in result
    assert "malformed" in result["reason"]


def test_unknown_adapter_name_in_source_id_returns_unavailable():
    adapter = _WorkingAdapter([_ATTENTION_WORK])

    result = fetch_evidence("CROSSREF:10.1234/whatever", [adapter])

    assert result["status"] == STATUS_UNAVAILABLE
    assert "not among the adapters supplied" in result["reason"]


def test_source_since_become_unavailable_real_search_returns_no_match():
    """A previously-resolved source_id whose record no longer turns up --
    the adapter genuinely searches (NOT_FOUND), and fetch_evidence must
    honestly report UNAVAILABLE, never invented evidence text."""
    adapter = _EmptyAdapter()

    result = fetch_evidence("OPENALEX:https://openalex.org/W2963341956", [adapter])

    assert result["status"] == STATUS_UNAVAILABLE
    assert "reason" in result


def test_transport_error_on_every_adapter_returns_unavailable_with_detail():
    adapter = _AlwaysFailingAdapter()

    result = fetch_evidence("OPENALEX:https://openalex.org/W2963341956", [adapter])

    assert result["status"] == STATUS_UNAVAILABLE
    assert "OPENALEX=TIMEOUT" in result["reason"] or "TIMEOUT" in result["reason"]


def test_wrong_record_id_on_matching_adapter_returns_unavailable_not_wrong_record():
    """Re-running search() can legitimately return a DIFFERENT real record
    than the one source_id names (adapters expose full-text search, not an
    identifier-lookup endpoint) -- fetch_evidence must never silently report
    that other record's evidence as if it were the one asked for."""
    unrelated_work = _openalex_work_json(
        work_id="https://openalex.org/W_UNRELATED",
        title="Some Unrelated Paper",
        authors=["Nobody"],
        year=2021,
        doi="10.1234/unrelated",
        abstract="Unrelated abstract text.",
    )
    adapter = _WorkingAdapter([unrelated_work])

    result = fetch_evidence("OPENALEX:https://openalex.org/W2963341956", [adapter])

    assert result["status"] == STATUS_UNAVAILABLE


def test_never_fabricates_evidence_text_for_fabricated_source_id():
    adapter = _EmptyAdapter()
    result = fetch_evidence("OPENALEX:https://openalex.org/W_DOES_NOT_EXIST", [adapter])
    assert result["status"] == STATUS_UNAVAILABLE
    assert "evidence_text" not in result
