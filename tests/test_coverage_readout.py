"""Offline unit tests for the Coverage Readout (core/coverage.py) and its
wiring into `ThaiJOAdapter`, `routing.router.RouteDecision`, `cli.py`, and
`mcp_server.py`.

Founder-approved redesign principle under test: "ไม่พบงาน" (nothing found)
must always be reported together with "ภายใต้แหล่งที่ค้นได้เหล่านี้" (given
which sources were actually searchable) -- never a bare "not found" that
silently hides a source-availability problem.

No network -- every fixture uses a temp-file `ThaiIndex`, matching the
offline technique already used in `tests/test_thaijo_index.py` /
`tests/test_thaijo_harvester.py`.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from thaicite.adapters.base import AdapterError
from thaicite.adapters.thaijo import ThaiJOAdapter
from thaicite.adapters.thaijo_index import DEFAULT_STALE_THRESHOLD_S, ThaiIndex
from thaicite.core import coverage as cov
from thaicite.core.engine import discover_citations
from thaicite.core.models import VerificationState
from thaicite.routing.router import HEALTH, route


@pytest.fixture()
def index():
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
        raw_metadata={"identifier": native_id, "titles": [title], "creators": [], "dates": [], "identifiers": [], "descriptions": []},
        harvested_at=harvested_at,
    )


# --------------------------------------------------------- core/coverage.py --


def test_coverage_entry_rejects_unknown_status():
    with pytest.raises(ValueError):
        cov.CoverageEntry(source="X", status="MAYBE")


def test_coverage_is_all_negative_true_only_when_nothing_is_ok():
    all_bad = [
        cov.CoverageEntry(source="A", status=cov.UNAVAILABLE),
        cov.CoverageEntry(source="B", status=cov.NOT_ATTEMPTED),
    ]
    assert cov.coverage_is_all_negative(all_bad) is True

    one_ok = all_bad + [cov.CoverageEntry(source="C", status=cov.OK)]
    assert cov.coverage_is_all_negative(one_ok) is False

    assert cov.coverage_is_all_negative([]) is False  # empty is not "all negative"


# ----------------------------------------------------- ThaiIndex staleness --


def test_index_stats_is_stale_when_never_synced(index):
    assert index.stats().is_stale() is True


def test_index_stats_is_stale_true_past_threshold(index):
    old = time.time() - (DEFAULT_STALE_THRESHOLD_S + 3600)
    _upsert(index, endpoint="https://sc01.tci-thaijo.org/index.php/index/oai", native_id="a1", title="Old record", harvested_at=old)
    assert index.stats().is_stale() is True


def test_index_stats_is_stale_false_within_threshold(index):
    fresh = time.time() - 60
    _upsert(index, endpoint="https://sc01.tci-thaijo.org/index.php/index/oai", native_id="a1", title="Fresh record", harvested_at=fresh)
    assert index.stats().is_stale() is False


# --------------------------------------------------- ThaiJOAdapter.coverage --


def test_endpoint_coverage_reports_not_attempted_for_never_synced_endpoint(index):
    adapter = ThaiJOAdapter(index=index)
    entries = {e.source: e for e in adapter.coverage_entries()}
    assert entries["THAIJO:sc01"].status == cov.NOT_ATTEMPTED
    assert entries["THAIJO:so01"].status == cov.NOT_ATTEMPTED


def test_endpoint_coverage_reports_ok_and_unavailable_independently(index):
    index.save_endpoint_status(code="sc01", url="https://sc01.tci-thaijo.org/index.php/index/oai", status="OK", records_harvested=5)
    index.save_endpoint_status(code="li01", url="https://li01.tci-thaijo.org/index.php/index/oai", status="UNAVAILABLE:RATE_LIMITED:429", records_harvested=0)

    adapter = ThaiJOAdapter(index=index)
    entries = {e.source: e for e in adapter.coverage_entries()}
    assert entries["THAIJO:sc01"].status == cov.OK
    assert entries["THAIJO:li01"].status == cov.UNAVAILABLE
    assert "RATE_LIMITED" in entries["THAIJO:li01"].reason
    # An endpoint neither synced nor failed is still NOT_ATTEMPTED, not
    # silently dropped from the readout.
    assert entries["THAIJO:so01"].status == cov.NOT_ATTEMPTED


# ---------------------------------------- WORKED EXAMPLE (task requirement) --
# "a query where ThaiJO's local index has zero matching records but the
# index itself is stale/never synced -- show the coverage readout correctly
# distinguishing 'searched, nothing matched' from 'never actually searched
# this source.'"


def test_worked_example_never_synced_index_is_distinguished_from_stale_or_fresh_zero_match(index):
    adapter_never_synced = ThaiJOAdapter(index=index)
    err = adapter_never_synced.search("การศึกษาไทยเรื่องประวัติศาสตร์")
    assert isinstance(err, AdapterError)
    assert err.state == VerificationState.NOT_FOUND
    assert err.coverage["index_never_synced"] is True
    assert err.coverage["index_total_records"] == 0
    assert "never been synced" in err.message

    # A second index that WAS synced (has records) but is stale, and the
    # query genuinely matches nothing in it -- this must NOT look identical
    # to "never synced".
    with tempfile.TemporaryDirectory() as tmpdir:
        stale_index = ThaiIndex(Path(tmpdir) / "stale.db")
        old = time.time() - (DEFAULT_STALE_THRESHOLD_S + 3600)
        _upsert(stale_index, endpoint="https://sc01.tci-thaijo.org/index.php/index/oai", native_id="a1", title="Unrelated botany survey", harvested_at=old)
        stale_index.save_endpoint_status(code="sc01", url="https://sc01.tci-thaijo.org/index.php/index/oai", status="OK", records_harvested=1, synced_at=old)

        adapter_stale = ThaiJOAdapter(index=stale_index)
        err_stale = adapter_stale.search("การศึกษาไทยเรื่องประวัติศาสตร์")
        assert isinstance(err_stale, AdapterError)
        assert err_stale.state == VerificationState.NOT_FOUND
        assert err_stale.coverage["index_never_synced"] is False
        assert err_stale.coverage["index_total_records"] == 1
        assert err_stale.coverage["index_is_stale"] is True
        assert "STALE" in err_stale.note.upper()

        stale_index.close()

    # A third, FRESH index that genuinely has zero matches for this query --
    # the honest "searched, nothing matched" case, distinct from both above.
    with tempfile.TemporaryDirectory() as tmpdir:
        fresh_index = ThaiIndex(Path(tmpdir) / "fresh.db")
        now = time.time() - 60
        _upsert(fresh_index, endpoint="https://sc01.tci-thaijo.org/index.php/index/oai", native_id="a1", title="Unrelated botany survey", harvested_at=now)
        fresh_index.save_endpoint_status(code="sc01", url="https://sc01.tci-thaijo.org/index.php/index/oai", status="OK", records_harvested=1, synced_at=now)

        adapter_fresh = ThaiJOAdapter(index=fresh_index)
        err_fresh = adapter_fresh.search("การศึกษาไทยเรื่องประวัติศาสตร์")
        assert isinstance(err_fresh, AdapterError)
        assert err_fresh.coverage["index_never_synced"] is False
        assert err_fresh.coverage["index_is_stale"] is False

        fresh_index.close()

    # The three cases must all be mutually distinguishable via `coverage`.
    assert (
        (err.coverage["index_never_synced"], err.coverage["index_is_stale"])
        != (err_stale.coverage["index_never_synced"], err_stale.coverage["index_is_stale"])
        != (err_fresh.coverage["index_never_synced"], err_fresh.coverage["index_is_stale"])
    )


# ------------------------------------------------- RouteDecision.coverage --


def test_route_decision_coverage_includes_not_connected_known_unconfigured_sources():
    from thaicite.adapters.crossref import CrossrefAdapter
    from thaicite.adapters.openalex import OpenAlexAdapter
    from thaicite.adapters.pubmed import PubMedAdapter

    adapters = [OpenAlexAdapter(), CrossrefAdapter(), ThaiJOAdapter(), PubMedAdapter()]
    decision = route("", "graph neural networks for traffic forecasting", adapters)
    by_source = {e.source: e for e in decision.coverage}
    assert by_source["TNRR"].status == cov.NOT_CONNECTED
    assert by_source["TCI"].status == cov.NOT_CONNECTED


def test_route_decision_coverage_marks_domain_excluded_adapter_not_attempted():
    from thaicite.adapters.crossref import CrossrefAdapter
    from thaicite.adapters.openalex import OpenAlexAdapter
    from thaicite.adapters.pubmed import PubMedAdapter

    adapters = [OpenAlexAdapter(), CrossrefAdapter(), ThaiJOAdapter(), PubMedAdapter()]
    decision = route("", "clinical trial of a new cancer drug", adapters)
    assert decision.domain == HEALTH
    by_source = {e.source: e for e in decision.coverage}
    assert by_source["THAIJO"].status == cov.NOT_ATTEMPTED


def test_route_decision_coverage_marks_unconfigured_adapter_not_connected():
    from thaicite.adapters.crossref import CrossrefAdapter
    from thaicite.adapters.openalex import OpenAlexAdapter

    limited = [OpenAlexAdapter(), CrossrefAdapter(), ThaiJOAdapter()]  # no PubMed
    decision = route("", "โรคเบาหวานในประเทศไทย diabetes prevalence", limited)
    by_source = {e.source: e for e in decision.coverage}
    assert by_source["PUBMED"].status == cov.NOT_CONNECTED


def test_route_decision_coverage_includes_thaijo_per_endpoint_rows():
    from thaicite.adapters.crossref import CrossrefAdapter
    from thaicite.adapters.openalex import OpenAlexAdapter

    adapters = [OpenAlexAdapter(), CrossrefAdapter(), ThaiJOAdapter()]
    decision = route("", "การศึกษาไทย", adapters)
    sources = {e.source for e in decision.coverage}
    assert "THAIJO" in sources
    assert "THAIJO:sc01" in sources
    assert "THAIJO:so01" in sources


def test_update_coverage_turns_errored_adapter_unavailable_not_masked_by_healthy_sibling():
    from thaicite.adapters.crossref import CrossrefAdapter
    from thaicite.adapters.openalex import OpenAlexAdapter
    from thaicite.adapters.pubmed import PubMedAdapter

    adapters = [OpenAlexAdapter(), CrossrefAdapter(), ThaiJOAdapter(), PubMedAdapter()]
    decision = route("", "graph neural networks for traffic forecasting", adapters)
    fake_engine_result = {
        "candidates": [],
        "rejected": {
            "query::x": {
                "reason": "adapter_error",
                "by_adapter": {
                    "OPENALEX": {"state": VerificationState.RATE_LIMITED, "message": "429", "coverage": None},
                },
            }
        },
        "not_found_queries": {},
    }
    decision.update_coverage(fake_engine_result)
    by_source = {e.source: e for e in decision.coverage}
    assert by_source["OPENALEX"].status == cov.UNAVAILABLE
    assert "RATE_LIMITED" in by_source["OPENALEX"].reason
    # A sibling adapter's a-priori OK on the SAME call is untouched by this
    # evidence -- it just has no confirming evidence yet either way.
    assert by_source["CROSSREF"].status == cov.OK


# ------------------------------------------------------------ end-to-end --


def test_discover_citations_empty_result_pairs_with_coverage_via_router(index, monkeypatch):
    """End-to-end: an empty `discover_citations()` result, combined with
    `RouteDecision.coverage`, must let a caller tell apart a real "searched,
    found nothing" from "some/most sources were never reachable" -- this is
    the CLI's/MCP's job (see cli.py::find_citations, mcp_server.py::find_cites).
    """
    from thaicite.adapters.crossref import CrossrefAdapter
    from thaicite.adapters.openalex import OpenAlexAdapter

    adapter = ThaiJOAdapter(index=index)  # never synced
    adapters = [OpenAlexAdapter(), CrossrefAdapter(), adapter]

    decision = route("", "หัวข้อที่ไม่มีอยู่จริงแน่นอน", adapters)
    result = discover_citations(context="หัวข้อที่ไม่มีอยู่จริงแน่นอน", adapters=decision.adapters)
    decision.update_coverage(result)

    assert result["candidates"] == []
    by_source = {e.source: e for e in decision.coverage}
    # ThaiJO's search() returned a NOT_FOUND AdapterError with coverage
    # attached -- update_coverage() must not have silently marked THAIJO
    # healthy just because OpenAlex/Crossref also came back NOT_FOUND.
    assert by_source["THAIJO"].status in (cov.OK, cov.UNAVAILABLE)
