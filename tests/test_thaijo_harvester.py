"""Offline unit tests for `thaicite.adapters.thaijo_harvester.ThaiJOHarvester`.

No live network calls -- `requests.get` is monkeypatched to return canned,
hand-built OAI-PMH XML responses, matching the offline technique already
used in `tests/test_adapters_offline.py`. The local index target is always
a temp-file `ThaiIndex`, never the repo's real default DB path.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from thaicite.adapters.thaijo_harvester import ThaiJOHarvester, _endpoint_url
from thaicite.adapters.thaijo_index import ThaiIndex
from thaicite.core.models import VerificationState


class _FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b""):
        self.status_code = status_code
        self.content = content


@pytest.fixture()
def harvester():
    with tempfile.TemporaryDirectory() as tmpdir:
        index = ThaiIndex(Path(tmpdir) / "index.db")
        h = ThaiJOHarvester(index=index, min_request_interval_s=0)
        yield h
        index.close()


_NO_RECORDS_MATCH_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <error code="noRecordsMatch">No matching records</error>
</OAI-PMH>
"""

_PROTOCOL_ERROR_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <error code="badVerb">Illegal OAI verb</error>
</OAI-PMH>
"""

_PAGE_1_WITH_TOKEN_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <ListRecords>
    <record>
      <header>
        <identifier>oai:tci-thaijo.org:article/3001</identifier>
      </header>
      <metadata>
        <oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
                    xmlns:dc="http://purl.org/dc/elements/1.1/">
          <dc:title>Paged medical communication study, page one</dc:title>
          <dc:creator>Somsri Somjai</dc:creator>
          <dc:date>2021-01-01</dc:date>
        </oai_dc:dc>
      </metadata>
    </record>
    <resumptionToken>cursor-abc-001</resumptionToken>
  </ListRecords>
</OAI-PMH>
"""

_PAGE_2_FINAL_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <ListRecords>
    <record>
      <header>
        <identifier>oai:tci-thaijo.org:article/3002</identifier>
      </header>
      <metadata>
        <oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
                    xmlns:dc="http://purl.org/dc/elements/1.1/">
          <dc:title>Paged medical communication study, page two</dc:title>
          <dc:creator>Somsri Somjai</dc:creator>
          <dc:date>2021-01-01</dc:date>
        </oai_dc:dc>
      </metadata>
    </record>
    <resumptionToken></resumptionToken>
  </ListRecords>
</OAI-PMH>
"""

_SINGLE_RECORD_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <ListRecords>
    <record>
      <header>
        <identifier>oai:tci-thaijo.org:article/5001</identifier>
      </header>
      <metadata>
        <oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
                    xmlns:dc="http://purl.org/dc/elements/1.1/">
          <dc:title>Law and gender equity in the workplace</dc:title>
          <dc:creator>J. Researcher</dc:creator>
          <dc:date>2022-03-01</dc:date>
          <dc:description>A study of workplace gender policy.</dc:description>
        </oai_dc:dc>
      </metadata>
    </record>
  </ListRecords>
</OAI-PMH>
"""


def test_endpoint_url_is_subdomain_based_not_path_based():
    # The corrected convention (this session's redesign): subdomain per
    # code, not the old `www.tci-thaijo.org/index.php/{code}/oai` path form.
    assert _endpoint_url("sc01") == "https://sc01.tci-thaijo.org/index.php/index/oai"
    assert _endpoint_url("so20") == "https://so20.tci-thaijo.org/index.php/index/oai"
    assert "www.tci-thaijo.org" not in _endpoint_url("sc01")


def test_sync_follows_resumption_token_across_pages(harvester, monkeypatch):
    def _fake_get(url, params=None, timeout=None):
        if params and params.get("resumptionToken") == "cursor-abc-001":
            return _FakeResponse(status_code=200, content=_PAGE_2_FINAL_XML)
        return _FakeResponse(status_code=200, content=_PAGE_1_WITH_TOKEN_XML)

    monkeypatch.setattr("thaicite.adapters.thaijo_harvester.requests.get", _fake_get)

    report = harvester.sync(endpoint_codes=["sc01"])
    assert report.total_records_harvested == 2
    assert report.statuses[0].status == "OK"
    assert harvester.index.stats().total_records == 2


def test_sync_respects_max_records_per_endpoint(harvester, monkeypatch):
    monkeypatch.setattr(
        "thaicite.adapters.thaijo_harvester.requests.get",
        lambda url, params=None, timeout=None: _FakeResponse(status_code=200, content=_PAGE_1_WITH_TOKEN_XML),
    )
    report = harvester.sync(endpoint_codes=["sc01"], max_records_per_endpoint=1)
    assert report.total_records_harvested == 1
    assert harvester.index.stats().total_records == 1


def test_sync_one_endpoint_failure_never_blocks_or_mislabels_another(harvester, monkeypatch):
    def _fake_get(url, params=None, timeout=None):
        if "sc01" in url:
            return _FakeResponse(status_code=404, content=b"")
        if "so01" in url:
            return _FakeResponse(status_code=200, content=_SINGLE_RECORD_XML)
        raise AssertionError(f"unexpected endpoint in test: {url}")

    monkeypatch.setattr("thaicite.adapters.thaijo_harvester.requests.get", _fake_get)

    report = harvester.sync(endpoint_codes=["sc01", "so01"])
    statuses = {s.code: s for s in report.statuses}
    assert statuses["sc01"].status.startswith("UNAVAILABLE:")
    assert statuses["so01"].status == "OK"
    assert statuses["so01"].records_harvested == 1
    assert harvester.index.stats().total_records == 1


def test_sync_no_records_match_is_ok_not_unavailable(harvester, monkeypatch):
    # A clean, successful harvest that legitimately found zero records is
    # not the same as the endpoint being unreachable/broken.
    monkeypatch.setattr(
        "thaicite.adapters.thaijo_harvester.requests.get",
        lambda url, params=None, timeout=None: _FakeResponse(status_code=200, content=_NO_RECORDS_MATCH_XML),
    )
    report = harvester.sync(endpoint_codes=["sc01"])
    assert report.statuses[0].status == "OK"
    assert report.statuses[0].records_harvested == 0


def test_sync_protocol_error_marks_endpoint_unavailable(harvester, monkeypatch):
    monkeypatch.setattr(
        "thaicite.adapters.thaijo_harvester.requests.get",
        lambda url, params=None, timeout=None: _FakeResponse(status_code=200, content=_PROTOCOL_ERROR_XML),
    )
    report = harvester.sync(endpoint_codes=["sc01"])
    assert report.statuses[0].status.startswith("UNAVAILABLE:")
    assert VerificationState.PARSER_ERROR in report.statuses[0].status


def test_sync_rate_limited_marks_endpoint_unavailable(harvester, monkeypatch):
    monkeypatch.setattr(
        "thaicite.adapters.thaijo_harvester.requests.get",
        lambda url, params=None, timeout=None: _FakeResponse(status_code=429),
    )
    report = harvester.sync(endpoint_codes=["sc01"])
    assert report.statuses[0].status.startswith("UNAVAILABLE:")
    assert VerificationState.RATE_LIMITED in report.statuses[0].status


def test_sync_is_idempotent_rerunning_does_not_duplicate(harvester, monkeypatch):
    monkeypatch.setattr(
        "thaicite.adapters.thaijo_harvester.requests.get",
        lambda url, params=None, timeout=None: _FakeResponse(status_code=200, content=_SINGLE_RECORD_XML),
    )
    harvester.sync(endpoint_codes=["so01"])
    harvester.sync(endpoint_codes=["so01"])
    assert harvester.index.stats().total_records == 1


def test_sync_upserted_record_is_searchable_after_harvest(harvester, monkeypatch):
    monkeypatch.setattr(
        "thaicite.adapters.thaijo_harvester.requests.get",
        lambda url, params=None, timeout=None: _FakeResponse(status_code=200, content=_SINGLE_RECORD_XML),
    )
    harvester.sync(endpoint_codes=["so01"])
    results = harvester.index.search("gender equity")
    assert len(results) == 1
    assert results[0].source_record_id == "oai:tci-thaijo.org:article/5001"


def test_sync_each_endpoint_gets_its_own_status_independent_of_others(harvester, monkeypatch):
    def _fake_get(url, params=None, timeout=None):
        if "sc01" in url:
            return _FakeResponse(status_code=200, content=_SINGLE_RECORD_XML)
        if "li01" in url:
            return _FakeResponse(status_code=429)
        if "so01" in url:
            return _FakeResponse(status_code=200, content=_NO_RECORDS_MATCH_XML)
        raise AssertionError(f"unexpected endpoint: {url}")

    monkeypatch.setattr("thaicite.adapters.thaijo_harvester.requests.get", _fake_get)
    report = harvester.sync(endpoint_codes=["sc01", "li01", "so01"])
    statuses = {s.code: s.status for s in report.statuses}
    assert statuses["sc01"] == "OK"
    assert statuses["li01"].startswith("UNAVAILABLE:")
    assert statuses["so01"] == "OK"
