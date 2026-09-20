"""Real adapter for ThaiJO (Thai Journals Online) via the OAI-PMH protocol.

STATUS AS OF 2026-09-20: NO endpoint listed below has been confirmed
working in ANY session, this one included. This session DID reach the
network and made exactly one single-attempt, read-only
`?verb=Identify` probe against the aggregator base URL
(`https://www.tci-thaijo.org/index.php/index/oai`) -- it returned HTTP 404
(an nginx-generated 404 page, not an OAI-PMH error envelope), the same
result an earlier session recorded. This confirms only that the
aggregator base URL as currently guessed is NOT live; the guessed
per-category URLs were NOT individually probed this session (to avoid
firing a burst of requests at a host whose rate-limit/ban behavior is
unknown), so their reachability remains genuinely unconfirmed either way.
Do not read the pagination/multi-endpoint work in this file as evidence
that any URL here actually responds -- it is protocol-shape correctness
only, verified-unreachable for the aggregator and still-unverified for
every per-category guess.

ASSUMED ENDPOINTS (none confirmed live -- see note above):
    Aggregator: https://www.tci-thaijo.org/index.php/index/oai
    Per-category (guessed, same OJS convention applied per subject code):
        https://www.tci-thaijo.org/index.php/<code>/oai
    where <code> is one of: sc01, li01..li05, ph01..ph05, he01..he05,
    so01..so20 -- codes carried over from this module's own prior research
    session, themselves unconfirmed against a live directory listing.
These are the classic OJS-convention OAI-PMH base URL
(`<site>/index.php/<context>/oai`) applied first to ThaiJO's top-level
"index" aggregator context (per ThaiJO's own public OAI-PMH documentation
for harvesters), and second to a guessed per-subject-category context,
because ThaiJO's real OAI-PMH structure is understood to be federated per
subject category rather than exposed as one working aggregator endpoint.
As of this adapter first being written (2026-09), a live probe of the
aggregator URL returned HTTP 404 -- the public tci-thaijo.org front end
appears to have been rebuilt on a platform that no longer exposes the
classic `/index.php/...` OJS path structure at the top level (its sitemap
now lists only extensionless routes like `/journals`, `/articles`,
`/en/...`). This could be: (a) OAI-PMH moved to a different base path,
(b) OAI-PMH is now served per individual journal/category only (not at
the aggregator level), or (c) OAI-PMH harvesting was disabled on the
public front end. None of this could be confirmed live in the session
that first wrote this file, and it still cannot be confirmed live as of
this revision (2026-09-20) -- no working OAI base URL, aggregator or
per-category, has ever been reached by this adapter in any session to
date.

Because no exact live base URL could be verified, this adapter is
implemented strictly against the DOCUMENTED OAI-PMH 2.0 protocol shape:
ListRecords with resumptionToken paging (this adapter now issues real
follow-up `verb=ListRecords&resumptionToken=<token>` requests and keeps
accumulating records until the response omits `<resumptionToken>`, a
configured record ceiling is reached, or a request fails), Dublin Core
`oai_dc` metadata, and the standard `<error code="...">` envelope for
protocol-level failures. This is correct request/response handling for
whichever base URLs (if any) turn out to be live once verified. Fix/trim
`endpoint_bases` (and re-verify each with a live `?verb=Identify` request)
before relying on this adapter in production.

Multi-endpoint behavior: `search()` queries every base URL in
`self.endpoint_bases` (default: the aggregator plus the guessed
per-category list above) independently -- one endpoint 404ing, timing
out, or returning a protocol error never blocks the others from being
tried. Records recovered from whichever endpoints actually respond are
aggregated and de-duplicated by OAI identifier before query-term
filtering. If every endpoint fails, `search()` returns a single
`AdapterError` summarizing the failures (rate-limit state takes priority,
then access-denied, then transport/timeout, then parser error, then
not-found -- never silently downgraded to "not found" when a real
endpoint failure occurred). Because this fans out to many endpoints, a
real run against this adapter with its default endpoint list is
necessarily slow: the same client-side rate-pacing guard (see below) is
applied between every single request, aggregator and per-category alike,
sequentially.

OAI-PMH has no native keyword-search verb -- it is a harvesting protocol,
not a search API. This adapter approximates "search" by harvesting pages
of records via ListRecords (now following resumptionToken across pages,
up to `max_records_to_scan` records total across all endpoints combined)
and filtering client-side on whether the query terms appear
(case-insensitively) in the Dublin Core title/description. This means an
OAI-PMH search here only sees whatever was harvested up to that ceiling;
it is not a full-corpus search, though the ceiling is now far higher than
the original single-page/50-record cap.

Rate policy (per ThaiJO's documented public rate limits): 10 requests/min/IP
normal, 3 requests/min/IP when throttled. This adapter applies a simple
client-side pacing guard -- a minimum interval between requests -- so it
does not trigger ThaiJO's own throttling by firing requests naively. The
guard now applies before every request this adapter issues (initial page,
resumptionToken follow-ups, and every per-endpoint attempt), not just once
per `search()` call.
"""

from __future__ import annotations

import time
from xml.etree import ElementTree as ET

import requests

from thaicite.adapters.base import AdapterError, RawRecord, SourceAdapter
from thaicite.core.models import Candidate, VerificationState

# See the ASSUMED ENDPOINTS note in the module docstring above.
_BASE_URL = "https://www.tci-thaijo.org/index.php/index/oai"

# Guessed per-subject-category context codes, per this module's own prior
# (unconfirmed) research -- see the module docstring. Never treated as
# settled fact; kept configurable via `endpoint_bases` on the constructor.
_CATEGORY_CODES: tuple[str, ...] = (
    "sc01",
    "li01", "li02", "li03", "li04", "li05",
    "ph01", "ph02", "ph03", "ph04", "ph05",
    "he01", "he02", "he03", "he04", "he05",
    *(f"so{n:02d}" for n in range(1, 21)),
)


def _default_endpoint_bases() -> list[str]:
    """The aggregator base URL plus one guessed per-category base per code.

    All of these are unverified -- see the module docstring's STATUS note.
    """
    return [_BASE_URL] + [
        f"https://www.tci-thaijo.org/index.php/{code}/oai" for code in _CATEGORY_CODES
    ]


_DEFAULT_TIMEOUT_S = 20
# 10 req/min normal ceiling -> at least 6s between requests. This adapter
# now issues many requests per search() call (resumptionToken pages times
# however many endpoints respond), and every single one of them respects
# this same pacing guard, sequentially.
_MIN_REQUEST_INTERVAL_S = 6.0
# Total records accumulated across ALL endpoints/pages combined, now that
# pagination is real. Configurable via the constructor; this default
# replaces the old silent 50-record single-page wall.
_MAX_RECORDS_TO_SCAN = 750

_OAI_NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}

# The priority order used to pick ONE state to report when every endpoint
# this adapter tried ended in an error -- a real rate-limit/access/timeout
# signal must never be silently outranked by a benign "not found".
_ERROR_STATE_PRIORITY = (
    VerificationState.RATE_LIMITED,
    VerificationState.ACCESS_DENIED,
    VerificationState.TIMEOUT,
    VerificationState.PARSER_ERROR,
    VerificationState.NOT_FOUND,
)


class ThaiJOAdapter(SourceAdapter):
    name = "THAIJO"

    def __init__(
        self,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        base_url: str = _BASE_URL,
        endpoint_bases: list[str] | None = None,
        min_request_interval_s: float = _MIN_REQUEST_INTERVAL_S,
        max_records_to_scan: int = _MAX_RECORDS_TO_SCAN,
    ) -> None:
        self.timeout_s = timeout_s
        # `base_url` is kept for backward compatibility (some callers may
        # construct this adapter pointing at one specific base). It is
        # always included as the first endpoint tried unless the caller
        # supplies an explicit `endpoint_bases` list of their own.
        self.base_url = base_url
        if endpoint_bases is not None:
            self.endpoint_bases = list(endpoint_bases)
        elif base_url != _BASE_URL:
            # Caller customized base_url explicitly and didn't also pass
            # endpoint_bases -- respect that single choice rather than
            # silently fanning out to the guessed category list too.
            self.endpoint_bases = [base_url]
        else:
            self.endpoint_bases = _default_endpoint_bases()
        self.min_request_interval_s = min_request_interval_s
        self.max_records_to_scan = max_records_to_scan
        self._last_request_monotonic: float | None = None

    def _pace(self) -> None:
        """Enforce the client-side minimum interval between requests."""
        if self._last_request_monotonic is None:
            return
        elapsed = time.monotonic() - self._last_request_monotonic
        remaining = self.min_request_interval_s - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _parse_page(
        self, root: ET.Element, endpoint_base: str
    ) -> tuple[list[dict], str | None, AdapterError | None]:
        """Parse one ListRecords response page.

        Returns (records, resumption_token, error). `error` is set instead
        of records when the page itself is a protocol-level failure (an
        OAI-PMH `<error>` element, or a missing `<ListRecords>` element).
        """
        error_el = root.find("oai:error", _OAI_NS)
        if error_el is not None:
            code = error_el.get("code", "")
            if code == "noRecordsMatch":
                return (
                    [],
                    None,
                    AdapterError(
                        state=VerificationState.NOT_FOUND,
                        adapter=self.name,
                        message=f"ThaiJO OAI-PMH ({endpoint_base}) reported "
                        "noRecordsMatch for this harvest.",
                    ),
                )
            return (
                [],
                None,
                AdapterError(
                    state=VerificationState.PARSER_ERROR,
                    adapter=self.name,
                    message=f"ThaiJO OAI-PMH ({endpoint_base}) returned "
                    f"protocol error code={code!r}: {error_el.text!r}",
                ),
            )

        list_records_el = root.find("oai:ListRecords", _OAI_NS)
        if list_records_el is None:
            return (
                [],
                None,
                AdapterError(
                    state=VerificationState.PARSER_ERROR,
                    adapter=self.name,
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

            records.append(
                {
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
                    "_endpoint": endpoint_base,
                }
            )

        token_el = list_records_el.find("oai:resumptionToken", _OAI_NS)
        token = None
        if token_el is not None and token_el.text and token_el.text.strip():
            token = token_el.text.strip()
        return records, token, None

    def _fetch_page(
        self, endpoint_base: str, params: dict
    ) -> tuple[ET.Element | None, AdapterError | None]:
        """Issue one paced HTTP request and return a parsed XML root, or an error."""
        self._pace()
        try:
            response = requests.get(endpoint_base, params=params, timeout=self.timeout_s)
        except requests.exceptions.Timeout:
            self._last_request_monotonic = time.monotonic()
            return None, AdapterError(
                state=VerificationState.TIMEOUT,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH request to {endpoint_base} timed out "
                f"after {self.timeout_s}s.",
            )
        except requests.exceptions.RequestException as exc:
            self._last_request_monotonic = time.monotonic()
            # Connection errors, DNS failures, etc. -- not a "not found",
            # a genuine failure to reach the source at all.
            return None, AdapterError(
                state=VerificationState.TIMEOUT,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH request to {endpoint_base} failed: {exc}",
            )
        self._last_request_monotonic = time.monotonic()

        if response.status_code == 429:
            return None, AdapterError(
                state=VerificationState.RATE_LIMITED,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH ({endpoint_base}) returned HTTP 429 "
                "(rate limited).",
            )
        if response.status_code in (401, 403):
            return None, AdapterError(
                state=VerificationState.ACCESS_DENIED,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH ({endpoint_base}) returned HTTP "
                f"{response.status_code} (access denied).",
            )
        if 500 <= response.status_code < 600:
            return None, AdapterError(
                state=VerificationState.TIMEOUT,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH ({endpoint_base}) returned HTTP "
                f"{response.status_code} (server error).",
            )
        if response.status_code != 200:
            return None, AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH ({endpoint_base}) returned unexpected "
                f"HTTP {response.status_code}. Base URL may be stale/unverified "
                "-- see module docstring.",
            )

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            return None, AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH ({endpoint_base}) response was not "
                f"valid XML: {exc}",
            )
        return root, None

    def _harvest_endpoint(self, endpoint_base: str, budget: int) -> tuple[list[dict], AdapterError | None]:
        """Harvest up to `budget` records from one endpoint, following resumptionToken.

        Returns whatever records were collected before any failure, plus an
        error if the endpoint ultimately failed (a `noRecordsMatch` on the
        first page also comes back as an error here, matching the previous
        per-adapter NOT_FOUND behavior for a single-endpoint case).
        """
        collected: list[dict] = []
        resumption_token: str | None = None
        if budget <= 0:
            return collected, None

        while True:
            params = (
                {"verb": "ListRecords", "resumptionToken": resumption_token}
                if resumption_token
                else {"verb": "ListRecords", "metadataPrefix": "oai_dc"}
            )
            root, fetch_err = self._fetch_page(endpoint_base, params)
            if fetch_err is not None:
                return collected, fetch_err

            page_records, token, page_err = self._parse_page(root, endpoint_base)
            if page_err is not None:
                return collected, page_err

            collected.extend(page_records)
            if len(collected) >= budget:
                break
            if not token:
                break
            resumption_token = token

        return collected, None

    def search(self, query: str) -> list[RawRecord] | AdapterError:
        combined: dict[str, dict] = {}
        endpoint_errors: list[AdapterError] = []

        for endpoint_base in self.endpoint_bases:
            remaining_budget = self.max_records_to_scan - len(combined)
            if remaining_budget <= 0:
                break
            records, err = self._harvest_endpoint(endpoint_base, remaining_budget)
            for rec in records:
                identifier = rec["identifier"]
                if identifier not in combined:
                    combined[identifier] = rec
            if err is not None:
                endpoint_errors.append(err)

        scanned = len(combined)
        query_terms = [t.lower() for t in query.split() if t]
        retrieved_at = time.time()
        records_out: list[RawRecord] = []
        for identifier, rec in combined.items():
            haystack = " ".join((rec.get("titles") or []) + (rec.get("descriptions") or [])).lower()
            if query_terms and not all(term in haystack for term in query_terms):
                continue
            raw_metadata = {k: v for k, v in rec.items() if k != "_endpoint"}
            records_out.append(
                RawRecord(
                    source_record_id=identifier,
                    raw_metadata=raw_metadata,
                    retrieved_at=retrieved_at,
                )
            )

        if records_out:
            return records_out

        if combined:
            # At least one endpoint yielded records, but none matched the
            # query terms -- a genuine "not found via this adapter" result.
            return AdapterError(
                state=VerificationState.NOT_FOUND,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH harvest returned records but none matched "
                f"query {query!r} (scanned={scanned} across "
                f"{len(self.endpoint_bases)} endpoint(s), max={self.max_records_to_scan}).",
            )

        if not endpoint_errors:
            # No endpoint produced records and none reported an error --
            # e.g. an empty endpoint list, or every endpoint's harvest
            # legitimately ended with zero records and no protocol error.
            return AdapterError(
                state=VerificationState.NOT_FOUND,
                adapter=self.name,
                message=f"ThaiJO OAI-PMH harvest returned no records for query "
                f"{query!r} across {len(self.endpoint_bases)} endpoint(s).",
            )

        # Every endpoint that was tried ended in an error. Report the
        # single highest-priority state so a real failure (rate limit,
        # access denied, transport/timeout, parser error) is never
        # silently outranked by a benign per-endpoint "not found".
        for state in _ERROR_STATE_PRIORITY:
            matches = [e for e in endpoint_errors if e.state == state]
            if not matches:
                continue
            sample = "; ".join(e.message for e in matches[:5])
            return AdapterError(
                state=state,
                adapter=self.name,
                message=f"All {len(self.endpoint_bases)} attempted endpoint(s) "
                f"failed for query {query!r}. Sample: {sample}",
            )

        # Unreachable in practice (endpoint_errors is non-empty and every
        # error's state is one of _ERROR_STATE_PRIORITY by construction),
        # but fail loudly rather than silently if it ever happens.
        raise AssertionError("endpoint_errors present but no priority state matched")

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
