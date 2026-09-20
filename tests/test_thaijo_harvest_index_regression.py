"""End-to-end regression: real ground-truth ThaiJO titles, harvested via the
real `ThaiJOHarvester.sync()` (against synthetic/fixture OAI-PMH XML, no
live network) into a temp SQLite `ThaiIndex`, then found again through
`ThaiIndex.search()` using real FTS5 + real Thai tokenization.

This is deliberately end-to-end across the harvest boundary -- the other
suites (`test_thaijo_harvester.py`, `test_thaijo_index.py`,
`test_thai_ground_truth_regression.py`) each already cover harvesting and
searching/relevance-gating SEPARATELY; this file is the one that proves the
two halves actually compose correctly for the 4 real titles the external
adversarial report named, going through OAI-PMH XML parsing -> upsert ->
FTS5 MATCH -> Thai tokenization, with nothing mocked except the HTTP
transport (`requests.get`, monkeypatched to return canned XML -- the same
offline technique `test_thaijo_harvester.py` already uses).

The 4 titles (from the task / external adversarial report):
  1. "สิทธิและหน้าที่ของภริยาตามกฎหมายอิสลาม..." (rights and duties of a wife
     under Islamic law)
  2. "สิทธิของภริยาในการหย่าและสิทธิที่พึงได้รับตามกฎหมายอิสลาม..." (a wife's
     right to divorce under Islamic law)
  3. "ผู้ไกล่เกลี่ยหญิงมุสลิมในระบบยุติธรรมพหุนิยม..." (female Muslim
     mediators in a pluralist justice system)
  4. "กฎหมายชารีอะห์กับผู้หญิงอาเจะห์..." (Sharia law and Acehnese women)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from thaicite.adapters.thaijo_harvester import ThaiJOHarvester
from thaicite.adapters.thaijo_index import ThaiIndex

TITLE_WIFE_RIGHTS_DUTIES = "สิทธิและหน้าที่ของภริยาตามกฎหมายอิสลามในสังคมไทยร่วมสมัย"
TITLE_DIVORCE_RIGHTS = "สิทธิของภริยาในการหย่าและสิทธิที่พึงได้รับตามกฎหมายอิสลาม"
TITLE_MEDIATORS = "ผู้ไกล่เกลี่ยหญิงมุสลิมในระบบยุติธรรมพหุนิยมของสามจังหวัดชายแดนใต้"
TITLE_ACEH_WOMEN = "กฎหมายชารีอะห์กับผู้หญิงอาเจะห์ในบริบทสังคมอินโดนีเซีย"

_FOUR_TITLES = (
    TITLE_WIFE_RIGHTS_DUTIES,
    TITLE_DIVORCE_RIGHTS,
    TITLE_MEDIATORS,
    TITLE_ACEH_WOMEN,
)


def _oai_record_xml(*, identifier: str, title: str, creator: str, description: str) -> str:
    """One <record> block, real OAI-PMH/Dublin-Core shape (matches the
    fixtures `_PAGE_1_WITH_TOKEN_XML`/`_SINGLE_RECORD_XML` already use in
    `test_thaijo_harvester.py`)."""
    return f"""
    <record>
      <header>
        <identifier>{escape(identifier)}</identifier>
      </header>
      <metadata>
        <oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
                    xmlns:dc="http://purl.org/dc/elements/1.1/">
          <dc:title>{escape(title)}</dc:title>
          <dc:creator>{escape(creator)}</dc:creator>
          <dc:date>2021-01-01</dc:date>
          <dc:description>{escape(description)}</dc:description>
        </oai_dc:dc>
      </metadata>
    </record>
    """


def _list_records_xml(records_xml: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">\n'
        "  <ListRecords>\n"
        f"{records_xml}"
        "    <resumptionToken></resumptionToken>\n"
        "  </ListRecords>\n"
        "</OAI-PMH>\n"
    ).encode("utf-8")


# One endpoint ("so01") carries all 4 ground-truth records, matching how a
# real Islamic-law/gender-topic journal would sit under ThaiJO's so-series
# (social-science) category codes per `thaijo_harvester.py`'s own docstring.
_FOUR_TITLES_XML = _list_records_xml(
    "".join(
        _oai_record_xml(
            identifier=f"oai:tci-thaijo.org:article/gt-{i}",
            title=title,
            creator="ผู้วิจัย ตัวอย่าง",
            description=f"บทความวิจัยนี้ศึกษา{title}อย่างละเอียดในบริบทกฎหมายอิสลามและสิทธิสตรี",
        )
        for i, title in enumerate(_FOUR_TITLES, start=1)
    )
)


class _FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b""):
        self.status_code = status_code
        self.content = content


@pytest.fixture()
def harvester_and_index():
    with tempfile.TemporaryDirectory() as tmpdir:
        index = ThaiIndex(Path(tmpdir) / "index.db")
        harvester = ThaiJOHarvester(index=index, min_request_interval_s=0)
        yield harvester, index
        index.close()


def test_harvest_the_4_ground_truth_titles_into_temp_index(harvester_and_index, monkeypatch):
    """The real `ThaiJOHarvester.sync()`, against a synthetic/fixture
    OAI-PMH XML response carrying the 4 real titles, upserts all 4 into the
    temp `ThaiIndex` -- no live network (`requests.get` is monkeypatched)."""
    harvester, index = harvester_and_index

    monkeypatch.setattr(
        "thaicite.adapters.thaijo_harvester.requests.get",
        lambda url, params=None, timeout=None: _FakeResponse(status_code=200, content=_FOUR_TITLES_XML),
    )

    report = harvester.sync(endpoint_codes=["so01"])

    assert report.total_records_harvested == 4
    assert report.statuses[0].status == "OK"
    assert index.stats().total_records == 4


def test_thaiindex_search_surfaces_on_topic_titles_for_plausible_thai_query(
    harvester_and_index, monkeypatch
):
    """End-to-end retrieval proof (the task's core requirement): after a
    real harvest into the temp index, `ThaiIndex.search()` with a plausible
    Thai topic query ("กฎหมายอิสลาม ผู้หญิง" -- Islamic law, women) surfaces
    at least the clearly on-topic titles among the 4, via real FTS5 MATCH +
    real pythainlp-backed Thai tokenization -- not a mocked/stubbed
    tokenizer or search layer."""
    harvester, index = harvester_and_index

    monkeypatch.setattr(
        "thaicite.adapters.thaijo_harvester.requests.get",
        lambda url, params=None, timeout=None: _FakeResponse(status_code=200, content=_FOUR_TITLES_XML),
    )
    harvester.sync(endpoint_codes=["so01"])

    results = index.search("กฎหมายอิสลาม ผู้หญิง")
    surfaced_titles = {r.raw_metadata["titles"][0] for r in results}

    assert surfaced_titles, "expected at least one match for a plausible on-topic Thai query"
    # The clearly on-topic titles (both explicitly mention "กฎหมายอิสลาม" --
    # Islamic law -- in their own title text) must be among the results.
    assert TITLE_WIFE_RIGHTS_DUTIES in surfaced_titles
    assert TITLE_DIVORCE_RIGHTS in surfaced_titles


def test_thaiindex_search_finds_mediators_and_aceh_titles_on_their_own_vocabulary(
    harvester_and_index, monkeypatch
):
    """The other 2 titles don't literally contain "กฎหมายอิสลาม" in their own
    title text (mediators/Aceh framing uses different vocabulary), so query
    them on their own real, on-topic terms instead -- still through the real
    harvested-then-searched pipeline, still real Thai tokenization/FTS5."""
    harvester, index = harvester_and_index

    monkeypatch.setattr(
        "thaicite.adapters.thaijo_harvester.requests.get",
        lambda url, params=None, timeout=None: _FakeResponse(status_code=200, content=_FOUR_TITLES_XML),
    )
    harvester.sync(endpoint_codes=["so01"])

    mediator_results = index.search("ผู้ไกล่เกลี่ย หญิงมุสลิม")
    mediator_titles = {r.raw_metadata["titles"][0] for r in mediator_results}
    assert TITLE_MEDIATORS in mediator_titles

    aceh_results = index.search("ชารีอะห์ อาเจะห์")
    aceh_titles = {r.raw_metadata["titles"][0] for r in aceh_results}
    assert TITLE_ACEH_WOMEN in aceh_titles


def test_thaiindex_search_result_is_reachable_via_thaijo_adapter_end_to_end(
    harvester_and_index, monkeypatch
):
    """One further hop end-to-end: `ThaiJOAdapter.search()` (the thin
    production wrapper `core.engine`/`routing.router` actually call) built
    on top of the SAME harvested temp index returns real `Candidate`
    objects for the on-topic query, through `to_candidates()` unmocked."""
    from thaicite.adapters.thaijo import ThaiJOAdapter

    harvester, index = harvester_and_index
    monkeypatch.setattr(
        "thaicite.adapters.thaijo_harvester.requests.get",
        lambda url, params=None, timeout=None: _FakeResponse(status_code=200, content=_FOUR_TITLES_XML),
    )
    harvester.sync(endpoint_codes=["so01"])

    adapter = ThaiJOAdapter(index=index)
    records = adapter.search("กฎหมายอิสลาม ผู้หญิง")
    assert isinstance(records, list), records  # sanity: not an AdapterError
    candidates = adapter.to_candidates(records)
    titles = {c.title for c in candidates}
    assert TITLE_WIFE_RIGHTS_DUTIES in titles or TITLE_DIVORCE_RIGHTS in titles
