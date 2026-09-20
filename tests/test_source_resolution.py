"""Offline unit tests for `core.source_resolution.resolve_source()` -- the
reality-anchor primitive (see that module's docstring).

No network call: adapters here are hand-authored stub adapters, matching the
`_WorkingAdapter`/`_AlwaysFailingAdapter` pattern already used in
`test_engine_offline.py`.
"""

from __future__ import annotations

import pytest

from thaicite.adapters.base import AdapterError, RawRecord
from thaicite.adapters.openalex import OpenAlexAdapter
from thaicite.core.models import VerificationState
from thaicite.core.source_resolution import (
    MATCH_BIBLIOGRAPHIC_FUZZY,
    MATCH_EXACT_IDENTIFIER,
    STATUS_CONFIRMED,
    STATUS_UNRESOLVED,
    resolve_source,
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
    """Returns a fixed set of real RawRecords -- no network call, same
    pattern as `test_engine_offline.py::_WorkingAdapter`."""

    name = "OPENALEX"

    def __init__(self, work_jsons):
        self._records = [
            RawRecord(source_record_id=w["id"], raw_metadata=w) for w in work_jsons
        ]

    def search(self, query):
        return list(self._records)

    def to_candidates(self, records):
        return _ADAPTER.to_candidates(records)


class _EmptyAdapter:
    """A real adapter that genuinely searched and found nothing -- the
    fabricated-title case's honest NOT_FOUND path."""

    name = "OPENALEX"

    def search(self, query):
        return AdapterError(
            state=VerificationState.NOT_FOUND,
            adapter=self.name,
            message="No records match.",
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


def test_missing_hint_raises_value_error():
    with pytest.raises(ValueError):
        resolve_source({}, [_WorkingAdapter([])])
    with pytest.raises(ValueError):
        resolve_source({"title": "   "}, [_WorkingAdapter([])])


def test_title_hint_resolves_to_source_confirmed_bibliographic_fuzzy():
    adapter = _WorkingAdapter([_ATTENTION_WORK])
    result = resolve_source({"title": "Attention is all you need"}, [adapter])

    assert result["status"] == STATUS_CONFIRMED
    assert result["source_id"] == "OPENALEX:https://openalex.org/W2963341956"
    assert result["identity"]["title"] == "Attention is All you Need"
    assert result["identity"]["source_adapter"] == "OPENALEX"
    assert result["match_confidence"] == MATCH_BIBLIOGRAPHIC_FUZZY


def test_doi_hint_resolves_via_exact_identifier_shortcut():
    doi = "10.48550/arxiv.1706.03762"
    adapter = _WorkingAdapter([_ATTENTION_WORK])
    result = resolve_source({"doi": doi}, [adapter])

    assert result["status"] == STATUS_CONFIRMED
    assert result["match_confidence"] == MATCH_EXACT_IDENTIFIER
    assert result["identity"]["doi"] == doi


def test_fabricated_title_resolves_unresolved_never_invents_a_source_id():
    """The core reality-anchor guarantee: a plausible-sounding fabricated
    title that no adapter can find must resolve to UNRESOLVED, honestly
    reporting coverage -- never a fabricated source_id."""
    adapter = _EmptyAdapter()
    result = resolve_source(
        {
            "title": (
                "Zorblatt Quinductive Flerbonoid Synthesis in Nonexistent "
                "Glimmerwood Fungal Networks"
            )
        },
        [adapter],
    )

    assert result["status"] == STATUS_UNRESOLVED
    assert "source_id" not in result
    assert result["coverage"]["entries"]
    statuses = {e["source"]: e["status"] for e in result["coverage"]["entries"]}
    assert statuses["OPENALEX"] == "SEARCHED_OK"


def test_identifier_hint_never_falls_back_to_fuzzy_match():
    """Regression: an adapter searched with a doi/pmid as the raw query text
    can return an UNRELATED real record that happens to clear the fuzzy
    title/author matcher (e.g. digit-token overlap from an embedded DOI in
    the other record's own metadata). Confirming that record would be a
    real record but the WRONG one -- a reality-anchor failure by a
    different name. An identifier-driven hint must only ever confirm via
    gate_g6_identity_match's exact-identifier shortcut."""
    unrelated_work = _openalex_work_json(
        work_id="https://openalex.org/W1111111",
        title="Some Unrelated Paper About Post-Quantum Cryptosystems",
        authors=["Someone Else"],
        year=2022,
        doi="10.1234/unrelated",
    )
    adapter = _WorkingAdapter([unrelated_work])

    result = resolve_source({"doi": "10.9999/does-not-match-anything"}, [adapter])

    assert result["status"] == STATUS_UNRESOLVED
    assert "source_id" not in result
