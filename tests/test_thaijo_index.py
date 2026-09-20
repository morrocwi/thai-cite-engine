"""Offline unit tests for `thaicite.adapters.thaijo_index.ThaiIndex`.

All tests use a temp-file-backed SQLite database (never `:memory:` alone
where a shared connection across cursors matters, though this module's
single-connection design would tolerate either) -- no network, no live
ThaiJO access.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from thaicite.adapters.thaijo_index import ThaiIndex


@pytest.fixture()
def index():
    with tempfile.TemporaryDirectory() as tmpdir:
        idx = ThaiIndex(Path(tmpdir) / "index.db")
        yield idx
        idx.close()


def _record(identifier="oai:tci-thaijo.org:article/1", title="A study of X", abstract="About X in Thailand."):
    return {
        "identifier": identifier,
        "titles": [title],
        "creators": ["A. Author"],
        "dates": ["2021-01-01"],
        "identifiers": [],
        "descriptions": [abstract],
        "source": [],
    }


def test_new_index_is_empty(index):
    assert index.is_empty() is True
    assert index.stats().total_records == 0


def test_upsert_then_search_finds_by_title_token(index):
    rec = _record(title="Communication problems in rural hospitals")
    index.upsert_record(
        endpoint="https://sc01.tci-thaijo.org/index.php/index/oai",
        native_id=rec["identifier"],
        title=rec["titles"][0],
        abstract=rec["descriptions"][0],
        raw_metadata=rec,
    )
    assert index.is_empty() is False
    results = index.search("communication")
    assert len(results) == 1
    assert results[0].source_record_id == rec["identifier"]
    assert results[0].raw_metadata["titles"] == [rec["titles"][0]]


def test_search_matches_abstract_token_too(index):
    rec = _record(title="An unrelated title", abstract="Discusses gender equity policy in depth.")
    index.upsert_record(
        endpoint="https://so01.tci-thaijo.org/index.php/index/oai",
        native_id=rec["identifier"],
        title=rec["titles"][0],
        abstract=rec["descriptions"][0],
        raw_metadata=rec,
    )
    results = index.search("gender equity")
    assert len(results) == 1


def test_search_or_semantics_one_matching_term_is_enough(index):
    rec = _record(title="Hospital communication study")
    index.upsert_record(
        endpoint="https://sc01.tci-thaijo.org/index.php/index/oai",
        native_id=rec["identifier"],
        title=rec["titles"][0],
        abstract=rec["descriptions"][0],
        raw_metadata=rec,
    )
    # "communication" matches, "zzznomatch" does not -- OR semantics means
    # this still hits (the old adapter's AND-over-raw-tokens would not).
    results = index.search("communication zzznomatch")
    assert len(results) == 1


def test_search_no_match_returns_empty_list(index):
    rec = _record()
    index.upsert_record(
        endpoint="https://sc01.tci-thaijo.org/index.php/index/oai",
        native_id=rec["identifier"],
        title=rec["titles"][0],
        abstract=rec["descriptions"][0],
        raw_metadata=rec,
    )
    assert index.search("completely unrelated botany survey") == []


def test_search_empty_query_returns_empty_list(index):
    rec = _record()
    index.upsert_record(
        endpoint="https://sc01.tci-thaijo.org/index.php/index/oai",
        native_id=rec["identifier"],
        title=rec["titles"][0],
        abstract=rec["descriptions"][0],
        raw_metadata=rec,
    )
    assert index.search("") == []
    assert index.search("   ") == []


def test_upsert_is_idempotent_keyed_on_endpoint_and_native_id(index):
    rec = _record(title="Original title")
    endpoint = "https://sc01.tci-thaijo.org/index.php/index/oai"
    index.upsert_record(
        endpoint=endpoint, native_id=rec["identifier"], title=rec["titles"][0],
        abstract=rec["descriptions"][0], raw_metadata=rec, harvested_at=1000.0,
    )
    assert index.stats().total_records == 1

    # Re-sync the same (endpoint, native_id) with an updated title and a
    # later harvested_at -- must REPLACE, not duplicate.
    rec2 = _record(title="Revised title")
    index.upsert_record(
        endpoint=endpoint, native_id=rec["identifier"], title=rec2["titles"][0],
        abstract=rec2["descriptions"][0], raw_metadata=rec2, harvested_at=2000.0,
    )
    assert index.stats().total_records == 1
    results = index.search("revised")
    assert len(results) == 1
    assert results[0].raw_metadata["titles"] == ["Revised title"]
    assert results[0].retrieved_at == 2000.0


def test_same_native_id_different_endpoints_do_not_collide(index):
    rec = _record(identifier="oai:tci-thaijo.org:article/999")
    index.upsert_record(
        endpoint="https://sc01.tci-thaijo.org/index.php/index/oai",
        native_id=rec["identifier"], title="Title from sc01",
        abstract="", raw_metadata=rec,
    )
    index.upsert_record(
        endpoint="https://so01.tci-thaijo.org/index.php/index/oai",
        native_id=rec["identifier"], title="Title from so01",
        abstract="", raw_metadata=rec,
    )
    assert index.stats().total_records == 2


def test_search_result_carries_endpoint_and_harvested_at_provenance(index):
    rec = _record()
    endpoint = "https://sc01.tci-thaijo.org/index.php/index/oai"
    now = time.time()
    index.upsert_record(
        endpoint=endpoint, native_id=rec["identifier"], title=rec["titles"][0],
        abstract=rec["descriptions"][0], raw_metadata=rec, harvested_at=now,
    )
    results = index.search("study")
    assert results[0].raw_metadata["_endpoint"] == endpoint
    assert results[0].raw_metadata["_harvested_at"] == now
    assert results[0].retrieved_at == now


def test_stats_reports_per_endpoint_counts(index):
    for i, endpoint in enumerate(
        ["https://sc01.tci-thaijo.org/index.php/index/oai"] * 2
        + ["https://so01.tci-thaijo.org/index.php/index/oai"]
    ):
        rec = _record(identifier=f"oai:tci-thaijo.org:article/{i}")
        index.upsert_record(
            endpoint=endpoint, native_id=rec["identifier"], title=rec["titles"][0],
            abstract=rec["descriptions"][0], raw_metadata=rec,
        )
    stats = index.stats()
    assert stats.total_records == 3
    assert stats.endpoints["https://sc01.tci-thaijo.org/index.php/index/oai"] == 2
    assert stats.endpoints["https://so01.tci-thaijo.org/index.php/index/oai"] == 1
