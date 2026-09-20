"""Real adapter for ThaiJO (Thai Journals Online) via the OAI-PMH protocol.

ASSUMED ENDPOINT (could not be confirmed live -- see note below):
    https://www.tci-thaijo.org/index.php/index/oai
This is the classic OJS-convention OAI-PMH base URL
(`<site>/index.php/<context>/oai`) applied to ThaiJO's top-level "index"
aggregator context, per ThaiJO's own public OAI-PMH documentation for
harvesters. As of this adapter being written (2026-09), a live probe of
this URL returned HTTP 404 -- the public tci-thaijo.org front end appears
to have been rebuilt on a platform that no longer exposes the classic
`/index.php/...` OJS path structure at the top level (its sitemap now lists
only extensionless routes like `/journals`, `/articles`, `/en/...`). This
could be: (a) OAI-PMH moved to a different base path, (b) OAI-PMH is now
served per individual journal only (not at the aggregator level), or (c)
OAI-PMH harvesting was disabled on the public front end. None of these
could be confirmed live in this session (no working OAI base URL was
found by probing plausible paths or by inspecting the sitemap/robots.txt).

Because the exact live base URL could not be verified, this adapter is
implemented strictly against the DOCUMENTED OAI-PMH 2.0 protocol shape
(ListRecords with resumption-token paging, Dublin Core `oai_dc` metadata,
the standard `<error code="...">` envelope for protocol-level failures) so
its request/response handling is correct once `_BASE_URL` below is
corrected to a live endpoint. Fix `_BASE_URL` (and re-verify with a live
`?verb=Identify` request) before relying on this adapter in production.

OAI-PMH has no native keyword-search verb -- it is a harvesting protocol,
not a search API. This adapter approximates "search" by harvesting a page
of records via ListRecords and filtering client-side on whether the query
terms appear (case-insensitively) in the Dublin Core title/description.
This means an OAI-PMH search here only sees whatever the harvested page
contains; it is not a full-corpus search.

Rate policy (per ThaiJO's documented public rate limits): 10 requests/min/IP
normal, 3 requests/min/IP when throttled. This adapter applies a simple
client-side pacing guard -- a minimum interval between requests -- so it
does not trigger ThaiJO's own throttling by firing requests naively.
"""

from __future__ import annotations

import time
from xml.etree import ElementTree as ET

import requests

from thaicite.adapters.base import AdapterError, RawRecord, SourceAdapter
from thaicite.core.models import Candidate, VerificationState

# See the ASSUMED ENDPOINT note in the module docstring above.
_BASE_URL = "https://www.tci-thaijo.org/index.php/index/oai"
_DEFAULT_TIMEOUT_S = 20
# 10 req/min normal ceiling -> at least 6s between requests, kept even
# though this adapter only ever issues one request per search() call, so
# repeated calls from the same process/instance stay paced.
_MIN_REQUEST_INTERVAL_S = 6.0
_MAX_RECORDS_TO_SCAN = 50

_OAI_NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}


class ThaiJOAdapter(SourceAdapter):
    name = "THAIJO"

    def __init__(
        self,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        base_url: str = _BASE_URL,
        min_request_interval_s: float = _MIN_REQUEST_INTERVAL_S,
    ) -> None:
        self.timeout_s = timeout_s
        self.base_url = base_url
        self.min_request_interval_s = min_request_interval_s
        self._last_request_monotonic: float | None = None

    def _pace(self) -> None:
        """Enforce the client-side minimum interval between requests."""
        if self._last_request_monotonic is None:
            return
        elapsed = time.monotonic() - self._last_request_monotonic
        remaining = self.min_request_interval_s - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def search(self, query: str) -> list[RawRecord] | AdapterError:
        self._pace()
        try:
            response = requests.get(
                self.base_url,
                params={"verb": "ListRecords", "metadataPrefix": "oai_dc"},
                timeout=self.timeout_s,
            )
        except requests.exceptions.Timeout:
            self._last_request_monotonic = time.monotonic()
            return AdapterError(
                state=VerificationState.TIMEOUT,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH request timed out after {self.timeout_s}s.",
            )
        except requests.exceptions.RequestException as exc:
            self._last_request_monotonic = time.monotonic()
            # Connection errors, DNS failures, etc. -- not a "not found",
            # a genuine failure to reach the source at all.
            return AdapterError(
                state=VerificationState.TIMEOUT,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH request failed: {exc}",
            )
        self._last_request_monotonic = time.monotonic()

        if response.status_code == 429:
            return AdapterError(
                state=VerificationState.RATE_LIMITED,
                adapter=self.name,
                message="ThaiJO OAI-PMH returned HTTP 429 (rate limited).",
            )
        if response.status_code in (401, 403):
            return AdapterError(
                state=VerificationState.ACCESS_DENIED,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH returned HTTP {response.status_code} "
                "(access denied).",
            )
        if 500 <= response.status_code < 600:
            return AdapterError(
                state=VerificationState.TIMEOUT,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH returned HTTP {response.status_code} "
                "(server error).",
            )
        if response.status_code != 200:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH returned unexpected HTTP "
                f"{response.status_code}. Base URL may be stale -- see "
                "module docstring.",
            )

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH response was not valid XML: {exc}",
            )

        # OAI-PMH protocol-level errors come back as HTTP 200 with an
        # <error> element -- these are real failures, never NOT_FOUND,
        # EXCEPT the documented "noRecordsMatch" code, which genuinely
        # means the harvest itself succeeded and found zero records.
        error_el = root.find("oai:error", _OAI_NS)
        if error_el is not None:
            code = error_el.get("code", "")
            if code == "noRecordsMatch":
                return AdapterError(
                    state=VerificationState.NOT_FOUND,
                    adapter=self.name,
                    message="ThaiJO OAI-PMH reported noRecordsMatch for this harvest.",
                )
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH returned protocol error "
                f"code={code!r}: {error_el.text!r}",
            )

        list_records_el = root.find("oai:ListRecords", _OAI_NS)
        if list_records_el is None:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message="ThaiJO OAI-PMH response missing expected "
                "<ListRecords> element.",
            )

        query_terms = [t.lower() for t in query.split() if t]
        retrieved_at = time.time()
        records: list[RawRecord] = []
        scanned = 0
        for record_el in list_records_el.findall("oai:record", _OAI_NS):
            if scanned >= _MAX_RECORDS_TO_SCAN:
                break
            scanned += 1

            header_el = record_el.find("oai:header", _OAI_NS)
            identifier_el = header_el.find("oai:identifier", _OAI_NS) if header_el is not None else None
            identifier = identifier_el.text if identifier_el is not None else None
            if not identifier:
                # A record with no OAI identifier cannot become a
                # traceable Candidate; skip it rather than inventing one.
                continue

            metadata_el = record_el.find("oai:metadata", _OAI_NS)
            dc_el = metadata_el.find("oai_dc:dc", _OAI_NS) if metadata_el is not None else None

            titles = (
                [t.text or "" for t in dc_el.findall("dc:title", _OAI_NS)]
                if dc_el is not None
                else []
            )
            descriptions = (
                [d.text or "" for d in dc_el.findall("dc:description", _OAI_NS)]
                if dc_el is not None
                else []
            )
            haystack = " ".join(titles + descriptions).lower()

            if query_terms and not all(term in haystack for term in query_terms):
                continue

            raw_metadata = {
                "identifier": identifier,
                "titles": titles,
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
                "descriptions": descriptions,
                "source": (
                    [s.text or "" for s in dc_el.findall("dc:source", _OAI_NS)]
                    if dc_el is not None
                    else []
                ),
            }
            records.append(
                RawRecord(
                    source_record_id=identifier,
                    raw_metadata=raw_metadata,
                    retrieved_at=retrieved_at,
                )
            )

        if not records:
            return AdapterError(
                state=VerificationState.NOT_FOUND,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH harvest returned records but none matched "
                f"query {query!r} in the scanned page "
                f"(scanned={scanned}, max={_MAX_RECORDS_TO_SCAN}).",
            )

        return records

    def to_candidates(self, records: list[RawRecord]) -> list[Candidate]:
        """Convert real ThaiJO OAI-PMH RawRecords into Candidate objects.

        Every field pulled here comes directly from the OAI-PMH Dublin
        Core response -- nothing is inferred or invented by this adapter.
        """
        candidates: list[Candidate] = []
        for rec in records:
            meta = rec.raw_metadata
            titles = meta.get("titles") or []
            title = titles[0] if titles else ""
            year: int | None = None
            for date_str in meta.get("dates") or []:
                token = date_str[:4]
                if token.isdigit():
                    year = int(token)
                    break

            doi = None
            url = None
            for ident in meta.get("identifiers") or []:
                lowered = ident.lower()
                if "doi.org" in lowered or lowered.startswith("10."):
                    doi = ident.split("doi.org/")[-1] if "doi.org" in lowered else ident
                elif lowered.startswith("http"):
                    url = url or ident

            descriptions = meta.get("descriptions") or []
            abstract = descriptions[0] if descriptions else None

            candidates.append(
                Candidate(
                    source_adapter=self.name,
                    source_record_id=rec.source_record_id,
                    title=title,
                    authors=meta.get("creators") or [],
                    year=year,
                    doi=doi,
                    url=url,
                    abstract=abstract,
                    raw_metadata=meta,
                    retrieved_at=rec.retrieved_at,
                )
            )
        return candidates
