"""Harvester for ThaiJO (Thai Journals Online) via the OAI-PMH protocol.

ROUND 3 REDESIGN (2026-09-20, founder-approved), rebuilt from scratch on
top of round 2's pagination/multi-endpoint code (adapted here, not
reinvented -- see `_harvest_endpoint`/`_fetch_page`/`_parse_page` below,
carried over from the previous `adapters/thaijo.py`).

WHY THIS FILE EXISTS -- OAI-PMH is a HARVESTING protocol, not a search API.
Round 2 treated `search(query)` as the unit of work: every call re-issued
ListRecords against every configured endpoint, paginated via
resumptionToken, up to a single SHARED budget (750 records) across ALL
endpoints combined, then filtered client-side on query terms. Two
structural problems fell out of that:

  1. Re-harvesting per query variant. `core.engine.discover_citations()`
     fans a topic out into a Support x Challenge query family
     (`routing/query_planner.py`) and calls `search()` once per query
     variant -- so a single discovery call could fire the full harvest
     sequence (up to ~111 endpoint attempts: 1 aggregator + ~31 category
     codes, times however many resumptionToken pages each needed) AGAIN
     for every variant, against a host with a real 10 req/min rate ceiling.
  2. Fixed endpoint order starves later endpoints. With one shared budget
     and endpoints tried in `endpoint_bases` order, the `so01`..`so20`
     (social-science/law/gender-topic) endpoints -- likely to hold the
     content most gender/law queries actually want -- were only reached
     once every earlier endpoint (sc01, li01-05, ph01-05, he01-05) had
     already been fully drained, or the shared 750-record ceiling was hit
     first.

The fix: split HARVESTING (this file) from SEARCHING (`thaijo_index.py`,
`thaijo.py`). `ThaiJOHarvester.sync()` is a separate, explicit,
operator-run step that walks every endpoint INDEPENDENTLY (its own
resumptionToken pagination, its own record budget, its own pass/fail
status -- one endpoint failing never blocks or mislabels another) and
upserts what it finds into a local SQLite+FTS5 snapshot
(`thaijo_index.py::ThaiIndex`). `ThaiJOAdapter.search()` then queries that
local snapshot instantly, with no network call and no re-harvesting per
query variant. A sync is a periodic/manual operator action (see
`--sync`/`run_sync_cli()` below), not something that runs inline inside
every citation search.

CORRECTED ENDPOINT FORMAT -- confirmed THIS session: the existing code
(round 2) AND its own test suite both assumed the classic OJS convention,
`https://www.tci-thaijo.org/index.php/{code}/oai`. That was never verified
live for any per-category code, and the one URL that WAS actually probed
(the bare aggregator, `https://www.tci-thaijo.org/index.php/index/oai`)
came back HTTP 404. ThaiJO's real OAI-PMH convention is SUBDOMAIN-based,
per category, not a path segment under one shared host:

    https://{code}.tci-thaijo.org/index.php/index/oai

e.g. `https://sc01.tci-thaijo.org/index.php/index/oai`. Every endpoint this
harvester builds by default uses this corrected form (`_endpoint_url()`
below).

LIVE PROBE RESULT (this session, 2026-09-20): ONE gentle, single-attempt,
read-only `?verb=Identify` request against `https://sc01.tci-thaijo.org/
index.php/index/oai` (10s timeout, no retry) returned **HTTP 200** with a
well-formed OAI-PMH `<Identify>` response body (`<repositoryName>Thai
Journals Online (ThaiJO)</repositoryName>`, `protocolVersion=2.0`,
`<baseURL>` echoing the exact URL requested). This confirms the corrected
subdomain-based convention is reachable for at least this one code. No
other code in `_CATEGORY_CODES` was probed this session (deliberately
conservative -- this project already hit external rate limits once this
session from over-eager live probing); treat every OTHER code's
reachability as still unconfirmed until `sync()` (or a further manual
`?verb=Identify` probe) actually records `OK` for it.

Rate policy (unchanged from round 2, per ThaiJO's documented public rate
limits): 10 requests/min/IP normal, 3 requests/min/IP when throttled. The
same client-side pacing guard applies before every single HTTP request
this harvester issues, across every endpoint and every resumptionToken
page, sequentially -- `sync()` over the full default endpoint set is
necessarily slow by design, matching the same constraint round 2 already
documented.

INCREMENTAL HARVESTING -- OAI-PMH's selective-harvesting `from`/`until`
params (`since=` in a friendlier `sync()` signature) would let a re-sync
only pull records changed since the last successful harvest of that
endpoint. NOT implemented in this pass -- `sync()` always does a full
ListRecords harvest per endpoint. This is a real, deliberate scope cut
(not an oversight): full re-harvests remain correct (upserts are
idempotent, keyed on (endpoint, native_id)), just not incrementally
efficient. Left as a documented follow-up.

Run a harvest:
    python -m thaicite.adapters.thaijo_harvester --sync
    python -m thaicite.adapters.thaijo_harvester --sync --codes sc01 so01 so02
    python -m thaicite.adapters.thaijo_harvester --sync --max-per-endpoint 200
See `README.md`/`docs/ARCHITECTURE_NOTE.md` for the documented CLI usage,
and `build_arg_parser()`/`run_sync_cli()` below for the programmatic form
(callable directly, e.g. from `cli.py`, without going through argv).
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import requests

from thaicite.adapters.base import AdapterError
from thaicite.adapters.thaijo_index import DEFAULT_INDEX_DB_PATH, ThaiIndex
from thaicite.core.models import VerificationState

# Category codes carried over unchanged from round 2's own prior research
# (never confirmed against a live ThaiJO directory listing in any session
# to date -- see module docstring's CORRECTED ENDPOINT FORMAT section).
_CATEGORY_CODES: tuple[str, ...] = (
    "sc01",
    "li01", "li02", "li03", "li04", "li05",
    "ph01", "ph02", "ph03", "ph04", "ph05",
    "he01", "he02", "he03", "he04", "he05",
    *(f"so{n:02d}" for n in range(1, 21)),
)

_DEFAULT_TIMEOUT_S = 20
_MIN_REQUEST_INTERVAL_S = 6.0  # 10 req/min ceiling -> at least 6s between requests

_OAI_NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}


def _endpoint_url(code: str) -> str:
    """The corrected, subdomain-based OAI-PMH base URL for one category code.

    See module docstring's CORRECTED ENDPOINT FORMAT section.
    """
    return f"https://{code}.tci-thaijo.org/index.php/index/oai"


def default_endpoint_codes() -> tuple[str, ...]:
    return _CATEGORY_CODES


@dataclass
class EndpointStatus:
    """Independent pass/fail record for ONE endpoint's harvest attempt.

    `status` is one of "OK", "UNAVAILABLE:<reason>", or "NOT_ATTEMPTED" --
    one endpoint's failure is captured here and here only; it must never
    change another endpoint's own `status`.
    """

    code: str
    url: str
    status: str = "NOT_ATTEMPTED"
    records_harvested: int = 0

    def mark_ok(self, records_harvested: int) -> None:
        self.status = "OK"
        self.records_harvested = records_harvested

    def mark_unavailable(self, reason: str) -> None:
        self.status = f"UNAVAILABLE:{reason}"


@dataclass
class HarvestReport:
    """The full result of one `sync()` call."""

    statuses: list[EndpointStatus] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    index_db_path: str = ""

    @property
    def total_records_harvested(self) -> int:
        return sum(s.records_harvested for s in self.statuses)

    @property
    def ok_endpoints(self) -> list[EndpointStatus]:
        return [s for s in self.statuses if s.status == "OK"]

    @property
    def unavailable_endpoints(self) -> list[EndpointStatus]:
        return [s for s in self.statuses if s.status.startswith("UNAVAILABLE:")]

    def summary_lines(self) -> list[str]:
        lines = [
            f"ThaiJO harvest: {len(self.ok_endpoints)}/{len(self.statuses)} endpoint(s) OK, "
            f"{self.total_records_harvested} record(s) upserted into {self.index_db_path}"
        ]
        for s in self.statuses:
            lines.append(f"  {s.code:6s} {s.status:40s} records={s.records_harvested} url={s.url}")
        return lines


class ThaiJOHarvester:
    """Harvests ThaiJO OAI-PMH endpoints into a local `ThaiIndex` snapshot."""

    def __init__(
        self,
        index: ThaiIndex | None = None,
        index_db_path: str | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        min_request_interval_s: float = _MIN_REQUEST_INTERVAL_S,
    ) -> None:
        self.index = index or ThaiIndex(index_db_path or DEFAULT_INDEX_DB_PATH)
        self.timeout_s = timeout_s
        self.min_request_interval_s = min_request_interval_s
        self._last_request_monotonic: float | None = None

    # -- pacing / HTTP / XML parsing (adapted from round 2's thaijo.py) ---

    def _pace(self) -> None:
        if self._last_request_monotonic is None:
            return
        elapsed = time.monotonic() - self._last_request_monotonic
        remaining = self.min_request_interval_s - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _fetch_page(
        self, endpoint_base: str, params: dict
    ) -> tuple[ET.Element | None, AdapterError | None]:
        self._pace()
        try:
            response = requests.get(endpoint_base, params=params, timeout=self.timeout_s)
        except requests.exceptions.Timeout:
            self._last_request_monotonic = time.monotonic()
            return None, AdapterError(
                state=VerificationState.TIMEOUT,
                adapter="THAIJO_HARVESTER",
                message=f"ThaiJO OAI-PMH request to {endpoint_base} timed out "
                f"after {self.timeout_s}s.",
            )
        except requests.exceptions.RequestException as exc:
            self._last_request_monotonic = time.monotonic()
            return None, AdapterError(
                state=VerificationState.TIMEOUT,
                adapter="THAIJO_HARVESTER",
                message=f"ThaiJO OAI-PMH request to {endpoint_base} failed: {exc}",
            )
        self._last_request_monotonic = time.monotonic()

        if response.status_code == 429:
            return None, AdapterError(
                state=VerificationState.RATE_LIMITED,
                adapter="THAIJO_HARVESTER",
                message=f"ThaiJO OAI-PMH ({endpoint_base}) returned HTTP 429 (rate limited).",
            )
        if response.status_code in (401, 403):
            return None, AdapterError(
                state=VerificationState.ACCESS_DENIED,
                adapter="THAIJO_HARVESTER",
                message=f"ThaiJO OAI-PMH ({endpoint_base}) returned HTTP "
                f"{response.status_code} (access denied).",
            )
        if 500 <= response.status_code < 600:
            return None, AdapterError(
                state=VerificationState.TIMEOUT,
                adapter="THAIJO_HARVESTER",
                message=f"ThaiJO OAI-PMH ({endpoint_base}) returned HTTP "
                f"{response.status_code} (server error).",
            )
        if response.status_code != 200:
            return None, AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter="THAIJO_HARVESTER",
                message=f"ThaiJO OAI-PMH ({endpoint_base}) returned unexpected "
                f"HTTP {response.status_code}.",
            )

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            return None, AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter="THAIJO_HARVESTER",
                message=f"ThaiJO OAI-PMH ({endpoint_base}) response was not valid XML: {exc}",
            )
        return root, None

    def _parse_page(
        self, root: ET.Element, endpoint_base: str
    ) -> tuple[list[dict], str | None, AdapterError | None]:
        error_el = root.find("oai:error", _OAI_NS)
        if error_el is not None:
            code = error_el.get("code", "")
            if code == "noRecordsMatch":
                return (
                    [],
                    None,
                    AdapterError(
                        state=VerificationState.NOT_FOUND,
                        adapter="THAIJO_HARVESTER",
                        message=f"ThaiJO OAI-PMH ({endpoint_base}) reported "
                        "noRecordsMatch for this harvest.",
                    ),
                )
            return (
                [],
                None,
                AdapterError(
                    state=VerificationState.PARSER_ERROR,
                    adapter="THAIJO_HARVESTER",
                    message=f"ThaiJO OAI-PMH ({endpoint_base}) returned protocol "
                    f"error code={code!r}: {error_el.text!r}",
                ),
            )

        list_records_el = root.find("oai:ListRecords", _OAI_NS)
        if list_records_el is None:
            return (
                [],
                None,
                AdapterError(
                    state=VerificationState.PARSER_ERROR,
                    adapter="THAIJO_HARVESTER",
                    message=f"ThaiJO OAI-PMH ({endpoint_base}) response missing "
                    "expected <ListRecords> element.",
                ),
            )

        records: list[dict] = []
        for record_el in list_records_el.findall("oai:record", _OAI_NS):
            header_el = record_el.find("oai:header", _OAI_NS)
            identifier_el = (
                header_el.find("oai:identifier", _OAI_NS) if header_el is not None else None
            )
            identifier = identifier_el.text if identifier_el is not None else None
            if not identifier:
                continue

            metadata_el = record_el.find("oai:metadata", _OAI_NS)
            dc_el = metadata_el.find("oai_dc:dc", _OAI_NS) if metadata_el is not None else None

            records.append(
                {
                    "identifier": identifier,
                    "titles": (
                        [t.text or "" for t in dc_el.findall("dc:title", _OAI_NS)]
                        if dc_el is not None
                        else []
                    ),
                    "creators": (
                        [c.text or "" for c in dc_el.findall("dc:creator", _OAI_NS)]
                        if dc_el is not None
                        else []
                    ),
                    "dates": (
                        [d.text or "" for d in dc_el.findall("dc:date", _OAI_NS)]
                        if dc_el is not None
                        else []
                    ),
                    "identifiers": (
                        [i.text or "" for i in dc_el.findall("dc:identifier", _OAI_NS)]
                        if dc_el is not None
                        else []
                    ),
                    "descriptions": (
                        [d.text or "" for d in dc_el.findall("dc:description", _OAI_NS)]
                        if dc_el is not None
                        else []
                    ),
                    "source": (
                        [s.text or "" for s in dc_el.findall("dc:source", _OAI_NS)]
                        if dc_el is not None
                        else []
                    ),
                }
            )

        token_el = list_records_el.find("oai:resumptionToken", _OAI_NS)
        token = None
        if token_el is not None and token_el.text and token_el.text.strip():
            token = token_el.text.strip()
        return records, token, None

    def _harvest_endpoint(
        self, endpoint_url: str, budget: int | None
    ) -> tuple[list[dict], AdapterError | None]:
        """Harvest one endpoint, following resumptionToken, up to `budget`
        records (None = no ceiling, harvest until the token is exhausted).
        """
        collected: list[dict] = []
        resumption_token: str | None = None

        while True:
            params = (
                {"verb": "ListRecords", "resumptionToken": resumption_token}
                if resumption_token
                else {"verb": "ListRecords", "metadataPrefix": "oai_dc"}
            )
            root, fetch_err = self._fetch_page(endpoint_url, params)
            if fetch_err is not None:
                return collected, fetch_err

            page_records, token, page_err = self._parse_page(root, endpoint_url)
            if page_err is not None:
                return collected, page_err

            collected.extend(page_records)
            if budget is not None and len(collected) >= budget:
                return collected[:budget], None
            if not token:
                return collected, None
            resumption_token = token

    # -- public sync --------------------------------------------------------

    def sync(
        self,
        endpoint_codes: list[str] | None = None,
        max_records_per_endpoint: int | None = None,
    ) -> HarvestReport:
        """Harvest every endpoint independently and upsert into `self.index`.

        Each endpoint's status is tracked on its OWN `EndpointStatus`; one
        endpoint failing (timeout, 404, protocol error, rate limit) never
        blocks or mislabels another endpoint's real status. Safely
        re-runnable: every upsert is keyed on (endpoint, native_id), so
        re-syncing the same endpoint again just refreshes `harvested_at` on
        unchanged records and adds any genuinely new ones.
        """
        codes = list(endpoint_codes) if endpoint_codes is not None else list(_CATEGORY_CODES)
        report = HarvestReport(index_db_path=str(self.index.db_path))

        for code in codes:
            url = _endpoint_url(code)
            status = EndpointStatus(code=code, url=url)
            report.statuses.append(status)

            records, err = self._harvest_endpoint(url, max_records_per_endpoint)

            for rec in records:
                titles = rec.get("titles") or []
                descriptions = rec.get("descriptions") or []
                self.index.upsert_record(
                    endpoint=url,
                    native_id=rec["identifier"],
                    title=titles[0] if titles else "",
                    abstract=descriptions[0] if descriptions else "",
                    raw_metadata=rec,
                )

            if err is not None and err.state != VerificationState.NOT_FOUND:
                # A genuine failure (timeout/rate-limit/access-denied/parser
                # error) -- whatever partial records were collected before
                # the failure were still upserted above (never discarded),
                # but the endpoint's status must say it did not complete
                # cleanly.
                status.mark_unavailable(f"{err.state}:{err.message}")
                status.records_harvested = len(records)
            elif err is not None:
                # noRecordsMatch on the very first page: a real, successful
                # harvest that legitimately found zero records -- not a
                # failure of the endpoint itself.
                status.mark_ok(0)
            else:
                status.mark_ok(len(records))

            # Persist this endpoint's status immediately (Coverage Readout,
            # core/coverage.py) -- so `ThaiJOAdapter.coverage()` can answer
            # "was this endpoint ever reachable" at QUERY time, even though
            # `search()` itself never touches the network (see this module's
            # docstring). One endpoint's write never depends on another's.
            self.index.save_endpoint_status(
                code=code,
                url=url,
                status=status.status,
                records_harvested=status.records_harvested,
                synced_at=time.time(),
            )

        report.finished_at = time.time()
        return report


# =============================================================================
# CLI entry point: `python -m thaicite.adapters.thaijo_harvester --sync`
# =============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="thaicite.adapters.thaijo_harvester",
        description="Harvest ThaiJO OAI-PMH endpoints into the local ThaiIndex snapshot.",
    )
    parser.add_argument(
        "--sync", action="store_true", required=True, help="Run a harvest (the only supported action)."
    )
    parser.add_argument(
        "--codes",
        nargs="*",
        default=None,
        help="Category codes to harvest (default: the full built-in set, see module docstring).",
    )
    parser.add_argument(
        "--max-per-endpoint",
        type=int,
        default=None,
        help="Cap records harvested per endpoint (default: no cap, harvest until resumptionToken is exhausted).",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to the local index database (default: <repo>/.thaicite_cache/thaijo_index.db).",
    )
    return parser


def run_sync_cli(argv: list[str] | None = None) -> int:
    """Programmatic entry point (also callable from `cli.py`)."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    harvester = ThaiJOHarvester(index_db_path=args.db_path)
    report = harvester.sync(endpoint_codes=args.codes, max_records_per_endpoint=args.max_per_endpoint)
    for line in report.summary_lines():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(run_sync_cli())
