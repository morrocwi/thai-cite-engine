"""Offline unit tests for `core.engine.resolve_citations()` failure isolation
and its wiring into `routing.router.route()`.

No network call: adapters here are either the real, unmocked
`OpenAlexAdapter`/`ThaiJOAdapter` fed hand-authored RawRecords via a stub
`.search()` (matching the `_StubAdapter` pattern already used in
`test_cite_use_gate.py`), or a deliberately-failing fake adapter whose
`.search()` always returns an `AdapterError` -- reproducing the founder's
"one adapter down must never block the others" requirement at the actual
`resolve_citations()` call, not just at `RouteDecision.update_track_status()`
(which `test_router.py` already covers with a synthetic engine-result dict).
"""

from __future__ import annotations

from thaicite.adapters.base import AdapterError, RawRecord
from thaicite.adapters.crossref import CrossrefAdapter
from thaicite.adapters.openalex import OpenAlexAdapter
from thaicite.adapters.pubmed import PubMedAdapter
from thaicite.adapters.thaijo import ThaiJOAdapter
from thaicite.core.engine import resolve_citations
from thaicite.core.models import VerificationState
from thaicite.routing.router import route

_ADAPTER = OpenAlexAdapter()


def _inverted_index(text: str) -> dict[str, list[int]]:
    index: dict[str, list[int]] = {}
    for pos, word in enumerate(text.split()):
        index.setdefault(word, []).append(pos)
    return index


def _openalex_work_json(*, work_id, title, authors, year, doi, abstract):
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


class _WorkingAdapter:
    """Returns a fixed set of real RawRecords -- no network call."""

    name = "OPENALEX"

    def __init__(self, work_jsons):
        self._records = [
            RawRecord(source_record_id=w["id"], raw_metadata=w) for w in work_jsons
        ]

    def search(self, query):
        return list(self._records)

    def to_candidates(self, records):
        return _ADAPTER.to_candidates(records)


class _AlwaysFailingAdapter:
    """Simulates a downed adapter -- every search() call returns a tagged
    AdapterError, per the base.py contract (never a silent empty list)."""

    def __init__(self, name: str, state: str = VerificationState.TIMEOUT):
        self.name = name
        self._state = state

    def search(self, query):
        return AdapterError(
            state=self._state, adapter=self.name, message=f"{self.name} is simulated-down."
        )

    def to_candidates(self, records):  # pragma: no cover - never called on error
        return []


def test_resolve_citations_one_failing_adapter_does_not_block_a_working_one():
    work_json = _openalex_work_json(
        work_id="https://openalex.org/W9001",
        title="Deep Residual Learning for Image Recognition",
        authors=["Kaiming He", "Xiangyu Zhang"],
        year=2016,
        doi="10.1109/CVPR.2016.90",
        abstract="We present a residual learning framework to ease the training of deep networks.",
    )
    working = _WorkingAdapter([work_json])
    failing = _AlwaysFailingAdapter("CROSSREF", state=VerificationState.TIMEOUT)

    result = resolve_citations(
        context="",
        queries=["He K, Zhang X (2016). Deep Residual Learning for Image Recognition."],
        adapters=[working, failing],
    )

    # The working adapter's real hit still reaches VERIFIED even though a
    # sibling adapter errored on the exact same query.
    assert len(result["verified"]) == 1
    assert result["verified"][0].work.state == VerificationState.VERIFIED


def test_resolve_citations_all_adapters_failing_surfaces_adapter_error_not_not_found():
    failing_a = _AlwaysFailingAdapter("CROSSREF", state=VerificationState.RATE_LIMITED)
    failing_b = _AlwaysFailingAdapter("PUBMED", state=VerificationState.ACCESS_DENIED)

    result = resolve_citations(
        context="", queries=["some claim nobody can verify right now"], adapters=[failing_a, failing_b]
    )

    assert result["verified"] == []
    assert not result["not_found_queries"]  # real errors must not be laundered into NOT_FOUND
    key = "query::some claim nobody can verify right now"
    assert key in result["rejected"]
    assert result["rejected"][key]["reason"] == "adapter_error"
    by_adapter = result["rejected"][key]["by_adapter"]
    assert by_adapter["CROSSREF"]["state"] == VerificationState.RATE_LIMITED
    assert by_adapter["PUBMED"]["state"] == VerificationState.ACCESS_DENIED


def test_resolve_citations_mixed_not_found_and_error_prefers_adapter_error_label():
    # One adapter genuinely found nothing (NOT_FOUND), another transport-
    # failed (TIMEOUT) -- a real transport failure present anywhere means
    # this must be reported as adapter_error, never silently as not-found.
    not_found_adapter = _AlwaysFailingAdapter("OPENALEX", state=VerificationState.NOT_FOUND)
    timeout_adapter = _AlwaysFailingAdapter("CROSSREF", state=VerificationState.TIMEOUT)

    result = resolve_citations(
        context="", queries=["an obscure claim"], adapters=[not_found_adapter, timeout_adapter]
    )
    assert "query::an obscure claim" in result["rejected"]
    assert result["rejected"]["query::an obscure claim"]["reason"] == "adapter_error"


def test_router_selected_adapters_then_resolve_citations_thai_first_and_isolated():
    """End-to-end: route() picks Thai-first order for a THAI-domain query,
    and a failing global adapter in that routed list still does not block a
    real ThaiJO-shaped hit from reaching VERIFIED."""
    thai_work_raw_metadata = {
        "identifier": "oai:tci-thaijo.org:article/3003",
        "titles": ["การศึกษาไทยเรื่องประวัติศาสตร์เชียงใหม่"],
        "creators": ["สมชาย ใจดี"],
        "dates": ["2021-01-01"],
        "identifiers": ["https://doi.org/10.5555/chiangmai-history"],
        "descriptions": ["งานวิจัยประวัติศาสตร์เชียงใหม่"],
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
            return ThaiJOAdapter().to_candidates(records)

    available = [
        OpenAlexAdapter(),
        CrossrefAdapter(),
        _ThaiWorkingAdapter(),
        PubMedAdapter(),
    ]
    decision = route("", "การศึกษาไทยเรื่องประวัติศาสตร์เชียงใหม่", available)
    assert decision.adapter_names[0] == "THAIJO"

    routed = [
        adapter if adapter.name != "THAIJO" else _ThaiWorkingAdapter()
        for adapter in decision.adapters
    ]
    # Replace whichever global adapter route() picked with a simulated-down
    # one, to prove ThaiJO's real hit is unaffected by a global failure.
    routed = [
        _AlwaysFailingAdapter(a.name, state=VerificationState.TIMEOUT)
        if a.name != "THAIJO"
        else a
        for a in routed
    ]

    query = "การศึกษาไทยเรื่องประวัติศาสตร์เชียงใหม่"
    result = resolve_citations(context="", queries=[query], adapters=routed)

    # The query never ends up bucketed purely as an adapter_error -- the
    # real ThaiJO hit was actually evaluated (verified or a real per-work
    # rejection), which proves the failing global adapters did not block
    # ThaiJO's candidate from reaching the gates at all.
    adapter_error_key = f"query::{query}"
    assert adapter_error_key not in result["rejected"]
    assert len(result["cite_uses"]) == 1
    assert result["cite_uses"][0].work.candidates[0].source_adapter == "THAIJO"
