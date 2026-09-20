"""Offline unit tests for the Crossref / ThaiJO / PubMed adapters.

No live network calls anywhere in this file. Two distinct offline
techniques are used, matching what each function actually needs:

  - `to_candidates()` is a pure function (RawRecord -> Candidate, no I/O)
    -- exercised directly against hand-authored, source-shaped
    `raw_metadata` dicts, matching each source's own documented response
    format (Crossref `message.items[]`, the OAI-PMH Dublin Core record
    shape this adapter's own `search()` already extracts into,  NCBI
    ESummary `result.uids`/`result.<uid>`).
  - Error-state mapping lives inside each adapter's `search()`, interleaved
    with the real HTTP call -- to unit-test that mapping without a live
    call, `requests.get` (or `requests.exceptions.*`) is monkeypatched to
    return/raise a canned, hand-built response for the duration of one
    test only, per pytest's `monkeypatch` fixture (undone automatically at
    teardown). This is the standard offline technique for testing an HTTP
    client's own status-code/exception branches; it never reaches a real
    host.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import requests

from thaicite.adapters.base import AdapterError, RawRecord
from thaicite.adapters.crossref import CrossrefAdapter
from thaicite.adapters.pubmed import PubMedAdapter
from thaicite.adapters.thaijo import ThaiJOAdapter
from thaicite.adapters.thaijo_index import ThaiIndex
from thaicite.core.models import VerificationState


class _FakeResponse:
    """Minimal stand-in for `requests.Response`, offline only."""

    def __init__(self, status_code: int = 200, json_data=None, content: bytes = b""):
        self.status_code = status_code
        self._json_data = json_data
        self.content = content

    def json(self):
        if self._json_data is None:
            raise ValueError("this fake response carries no JSON body")
        return self._json_data


# =============================================================================
# Crossref
# =============================================================================


def _crossref_item(
    *, doi="10.5555/example1", title="A Study of Something", given="Jane", family="Doe", year=2021
):
    return {
        "DOI": doi,
        "title": [title],
        "author": [{"given": given, "family": family}],
        "issued": {"date-parts": [[year]]},
        "ISSN": ["1234-5678"],
        "URL": f"https://doi.org/{doi}",
        "abstract": "An abstract about something interesting.",
    }


def test_crossref_to_candidates_pure_function_real_field_shape():
    item = _crossref_item()
    record = RawRecord(source_record_id=item["DOI"], raw_metadata=item)
    candidates = CrossrefAdapter().to_candidates([record])
    assert len(candidates) == 1
    c = candidates[0]
    assert c.source_adapter == "CROSSREF"
    assert c.doi == "10.5555/example1"
    assert c.title == "A Study of Something"
    assert c.authors == ["Jane Doe"]
    assert c.year == 2021
    assert c.issn == "1234-5678"
    assert c.url == "https://doi.org/10.5555/example1"


def test_crossref_to_candidates_missing_optional_fields_do_not_crash():
    item = {"DOI": "10.5555/bare", "title": [], "author": [], "issued": {}}
    record = RawRecord(source_record_id=item["DOI"], raw_metadata=item)
    candidates = CrossrefAdapter().to_candidates([record])
    assert len(candidates) == 1
    c = candidates[0]
    assert c.title == ""
    assert c.authors == []
    assert c.year is None
    assert c.issn is None


def test_crossref_search_maps_429_to_rate_limited(monkeypatch):
    monkeypatch.setattr(
        "thaicite.adapters.crossref.requests.get",
        lambda *a, **k: _FakeResponse(status_code=429),
    )
    result = CrossrefAdapter().search("some query")
    assert isinstance(result, AdapterError)
    assert result.state == VerificationState.RATE_LIMITED


def test_crossref_search_maps_403_to_access_denied(monkeypatch):
    monkeypatch.setattr(
        "thaicite.adapters.crossref.requests.get",
        lambda *a, **k: _FakeResponse(status_code=403),
    )
    result = CrossrefAdapter().search("some query")
    assert isinstance(result, AdapterError)
    assert result.state == VerificationState.ACCESS_DENIED


def test_crossref_search_maps_500_to_timeout_not_not_found(monkeypatch):
    # A real server error must never collapse into NOT_FOUND.
    monkeypatch.setattr(
        "thaicite.adapters.crossref.requests.get",
        lambda *a, **k: _FakeResponse(status_code=503),
    )
    result = CrossrefAdapter().search("some query")
    assert isinstance(result, AdapterError)
    assert result.state == VerificationState.TIMEOUT


def test_crossref_search_maps_bad_json_to_parser_error(monkeypatch):
    monkeypatch.setattr(
        "thaicite.adapters.crossref.requests.get",
        lambda *a, **k: _FakeResponse(status_code=200, json_data=None),
    )
    result = CrossrefAdapter().search("some query")
    assert isinstance(result, AdapterError)
    assert result.state == VerificationState.PARSER_ERROR


def test_crossref_search_maps_empty_items_to_not_found(monkeypatch):
    monkeypatch.setattr(
        "thaicite.adapters.crossref.requests.get",
        lambda *a, **k: _FakeResponse(status_code=200, json_data={"message": {"items": []}}),
    )
    result = CrossrefAdapter().search("some query")
    assert isinstance(result, AdapterError)
    assert result.state == VerificationState.NOT_FOUND


def test_crossref_search_timeout_exception_maps_to_timeout_state(monkeypatch):
    def _raise_timeout(*a, **k):
        raise requests.exceptions.Timeout("simulated timeout")

    monkeypatch.setattr("thaicite.adapters.crossref.requests.get", _raise_timeout)
    result = CrossrefAdapter().search("some query")
    assert isinstance(result, AdapterError)
    assert result.state == VerificationState.TIMEOUT


def test_crossref_search_real_success_shape_returns_raw_records(monkeypatch):
    payload = {"message": {"items": [_crossref_item(doi="10.5555/ok")]}}
    monkeypatch.setattr(
        "thaicite.adapters.crossref.requests.get",
        lambda *a, **k: _FakeResponse(status_code=200, json_data=payload),
    )
    result = CrossrefAdapter().search("some query")
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].source_record_id == "10.5555/ok"


# =============================================================================
# ThaiJO -- Harvester + Local Index + thin adapter (round 3 redesign, 2026-09-20)
# =============================================================================
#
# `ThaiJOAdapter.search()` no longer touches the network at all -- it queries
# a local `ThaiIndex` (SQLite+FTS5) snapshot. So these tests split cleanly:
#   - `ThaiJOAdapter` tests here use a real temp-file-backed `ThaiIndex`
#     (pre-populated directly via `upsert_record`) -- no HTTP, no XML.
#   - `ThaiJOHarvester`/`ThaiIndex` HTTP+XML+pagination/endpoint-independence
#     tests live in `tests/test_thaijo_harvester.py` and
#     `tests/test_thaijo_index.py`.


@pytest.fixture()
def temp_index():
    with tempfile.TemporaryDirectory() as tmpdir:
        idx = ThaiIndex(Path(tmpdir) / "thaijo_index.db")
        yield idx
        idx.close()


def _thaijo_raw_metadata(
    *,
    identifier="oai:tci-thaijo.org:article/1001",
    title="Communication problems between medical staff and patients",
    creators=("Somchai Jaidee",),
    dates=("2020-05-01",),
    identifiers=("https://doi.org/10.5555/thai-study",),
    descriptions=("A study of hospital communication in Thailand.",),
):
    return {
        "identifier": identifier,
        "titles": [title],
        "creators": list(creators),
        "dates": list(dates),
        "identifiers": list(identifiers),
        "descriptions": list(descriptions),
        "source": [],
    }


def test_thaijo_to_candidates_pure_function_real_field_shape():
    # This is the exact raw_metadata shape ThaiIndex.search() reconstructs
    # from a harvested record -- reproduced by hand here so to_candidates()
    # is exercised without going through search()/ThaiIndex at all.
    raw_metadata = _thaijo_raw_metadata()
    record = RawRecord(source_record_id=raw_metadata["identifier"], raw_metadata=raw_metadata)
    candidates = ThaiJOAdapter(index=ThaiIndex(":memory:")).to_candidates([record])
    assert len(candidates) == 1
    c = candidates[0]
    assert c.source_adapter == "THAIJO"
    assert c.title == "Communication problems between medical staff and patients"
    assert c.authors == ["Somchai Jaidee"]
    assert c.year == 2020
    assert c.doi == "10.5555/thai-study"
    assert c.abstract == "A study of hospital communication in Thailand."


def test_thaijo_to_candidates_url_identifier_without_doi():
    raw_metadata = _thaijo_raw_metadata(
        identifier="oai:tci-thaijo.org:article/2002",
        title="Some other article",
        creators=(),
        dates=(),
        identifiers=("https://tci-thaijo.org/index.php/journal/article/view/2002",),
        descriptions=(),
    )
    record = RawRecord(source_record_id=raw_metadata["identifier"], raw_metadata=raw_metadata)
    candidates = ThaiJOAdapter(index=ThaiIndex(":memory:")).to_candidates([record])
    c = candidates[0]
    assert c.doi is None
    assert c.url == "https://tci-thaijo.org/index.php/journal/article/view/2002"


def test_thaijo_search_on_never_synced_index_is_honest_not_a_source_notfound(temp_index):
    adapter = ThaiJOAdapter(index=temp_index)
    result = adapter.search("communication medical")
    assert isinstance(result, AdapterError)
    assert result.state == VerificationState.NOT_FOUND
    # Must explicitly distinguish "local snapshot never synced" from a real
    # source-level "no such record" -- never silently collapsed together.
    assert "never been synced" in result.message
    assert "not" in result.note.lower() and "source-level" in result.note.lower()


def test_thaijo_search_real_success_shape_filters_via_fts(temp_index):
    raw_metadata = _thaijo_raw_metadata()
    temp_index.upsert_record(
        endpoint="https://sc01.tci-thaijo.org/index.php/index/oai",
        native_id=raw_metadata["identifier"],
        title=raw_metadata["titles"][0],
        abstract=raw_metadata["descriptions"][0],
        raw_metadata=raw_metadata,
    )
    adapter = ThaiJOAdapter(index=temp_index)
    result = adapter.search("communication medical")
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].source_record_id == "oai:tci-thaijo.org:article/1001"
    # Provenance carried through so a caller can see this is a snapshot
    # readout, not a live guarantee.
    assert "_endpoint" in result[0].raw_metadata
    assert "_harvested_at" in result[0].raw_metadata


def test_thaijo_search_or_semantics_matches_on_any_query_term(temp_index):
    # OR-semantics (not the old adapter's AND-over-raw-split-tokens): a
    # query with one matching term and one non-matching term still hits.
    raw_metadata = _thaijo_raw_metadata()
    temp_index.upsert_record(
        endpoint="https://sc01.tci-thaijo.org/index.php/index/oai",
        native_id=raw_metadata["identifier"],
        title=raw_metadata["titles"][0],
        abstract=raw_metadata["descriptions"][0],
        raw_metadata=raw_metadata,
    )
    adapter = ThaiJOAdapter(index=temp_index)
    result = adapter.search("communication nonexistentxyzterm")
    assert isinstance(result, list)
    assert len(result) == 1


def test_thaijo_search_no_fts_match_is_not_found(temp_index):
    raw_metadata = _thaijo_raw_metadata()
    temp_index.upsert_record(
        endpoint="https://sc01.tci-thaijo.org/index.php/index/oai",
        native_id=raw_metadata["identifier"],
        title=raw_metadata["titles"][0],
        abstract=raw_metadata["descriptions"][0],
        raw_metadata=raw_metadata,
    )
    adapter = ThaiJOAdapter(index=temp_index)
    result = adapter.search("completely unrelated botany survey")
    assert isinstance(result, AdapterError)
    assert result.state == VerificationState.NOT_FOUND


def test_thaijo_adapter_default_db_path_is_under_repo_not_hardcoded_elsewhere():
    from thaicite.adapters.thaijo_index import DEFAULT_INDEX_DB_PATH

    assert DEFAULT_INDEX_DB_PATH.is_absolute()
    assert ".thaicite_cache" in DEFAULT_INDEX_DB_PATH.parts
    # Must resolve under this repo checkout, never some fixed path outside
    # the user's own control (e.g. never under /etc, /var, or a bare "/").
    repo_root = Path(__file__).resolve().parents[1]
    assert str(DEFAULT_INDEX_DB_PATH).startswith(str(repo_root))



# =============================================================================
# PubMed (NCBI E-Utilities: ESearch + ESummary)
# =============================================================================


def _pubmed_esummary_item(
    *, pmid="12345678", title="A Clinical Trial", author_name="Smith J", doi="10.1/pm1", year_str="2019 Jan"
):
    return {
        "title": title,
        "authors": [{"name": author_name}],
        "articleids": [
            {"idtype": "doi", "value": doi},
            {"idtype": "pmc", "value": "PMC1234567"},
        ],
        "pubdate": year_str,
        "issn": "9999-0000",
    }


def test_pubmed_to_candidates_pure_function_real_field_shape():
    item = _pubmed_esummary_item()
    record = RawRecord(source_record_id="12345678", raw_metadata=item)
    candidates = PubMedAdapter().to_candidates([record])
    assert len(candidates) == 1
    c = candidates[0]
    assert c.source_adapter == "PUBMED"
    assert c.title == "A Clinical Trial"
    assert c.authors == ["Smith J"]
    assert c.doi == "10.1/pm1"
    assert c.pmid == "12345678"
    assert c.pmcid == "PMC1234567"
    assert c.year == 2019
    assert c.issn == "9999-0000"
    assert c.url == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    assert c.abstract is None  # ESummary never carries abstract text


def test_pubmed_to_candidates_missing_optional_fields_do_not_crash():
    item = {"title": "", "authors": [], "articleids": [], "pubdate": ""}
    record = RawRecord(source_record_id="99999999", raw_metadata=item)
    candidates = PubMedAdapter().to_candidates([record])
    c = candidates[0]
    assert c.doi is None
    assert c.pmcid is None
    assert c.year is None
    assert c.authors == []


def test_pubmed_search_esearch_empty_idlist_maps_to_not_found(monkeypatch):
    monkeypatch.setattr(
        "thaicite.adapters.pubmed.requests.get",
        lambda *a, **k: _FakeResponse(
            status_code=200, json_data={"esearchresult": {"idlist": []}}
        ),
    )
    result = PubMedAdapter().search("some query")
    assert isinstance(result, AdapterError)
    assert result.state == VerificationState.NOT_FOUND


def test_pubmed_search_esearch_429_maps_to_rate_limited(monkeypatch):
    monkeypatch.setattr(
        "thaicite.adapters.pubmed.requests.get",
        lambda *a, **k: _FakeResponse(status_code=429),
    )
    result = PubMedAdapter().search("some query")
    assert isinstance(result, AdapterError)
    assert result.state == VerificationState.RATE_LIMITED


def test_pubmed_search_esearch_malformed_json_maps_to_parser_error(monkeypatch):
    monkeypatch.setattr(
        "thaicite.adapters.pubmed.requests.get",
        lambda *a, **k: _FakeResponse(status_code=200, json_data={"unexpected": "shape"}),
    )
    result = PubMedAdapter().search("some query")
    assert isinstance(result, AdapterError)
    assert result.state == VerificationState.PARSER_ERROR


def test_pubmed_search_esummary_error_after_valid_esearch_propagates(monkeypatch):
    # First call (ESearch) succeeds with one PMID; second call (ESummary)
    # comes back access-denied -- the overall search() must surface THAT
    # error, not silently treat it as NOT_FOUND.
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(
                status_code=200, json_data={"esearchresult": {"idlist": ["1"]}}
            )
        return _FakeResponse(status_code=403)

    monkeypatch.setattr("thaicite.adapters.pubmed.requests.get", fake_get)
    result = PubMedAdapter().search("some query")
    assert isinstance(result, AdapterError)
    assert result.state == VerificationState.ACCESS_DENIED


def test_pubmed_search_real_success_shape_returns_raw_records(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(
                status_code=200, json_data={"esearchresult": {"idlist": ["12345678"]}}
            )
        return _FakeResponse(
            status_code=200,
            json_data={
                "result": {
                    "uids": ["12345678"],
                    "12345678": _pubmed_esummary_item(),
                }
            },
        )

    monkeypatch.setattr("thaicite.adapters.pubmed.requests.get", fake_get)
    result = PubMedAdapter().search("some query")
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].source_record_id == "12345678"
