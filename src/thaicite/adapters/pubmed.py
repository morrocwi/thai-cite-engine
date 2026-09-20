"""Real adapter for NCBI E-Utilities (PubMed) at
https://eutils.ncbi.nlm.nih.gov/entrez/eutils/ -- ESearch to resolve a query
to PMIDs, then ESummary to fetch structured metadata for those PMIDs.

No authentication required for low-volume use. An optional NCBI API key
(`THAICITE_NCBI_API_KEY` environment variable) is read and appended as the
`api_key` param if set -- this raises the rate ceiling per NCBI's own docs
but is never required and never hardcoded.

This is a REAL HTTP integration -- it is not mocked, and a genuine transport
failure must map to a distinct error state (RATE_LIMITED / TIMEOUT /
ACCESS_DENIED / PARSER_ERROR), never to NOT_FOUND.

PMC full-text fetching is OUT OF SCOPE for this adapter -- when ESummary
metadata includes a PMCID article-id, it is surfaced as `Candidate.pmcid`
so a later, separate PMC full-text fetcher can pick it up; nothing here
retrieves PMC full text itself.

Note: THAICITE_CONTACT_EMAIL is not used by this adapter -- NCBI E-Utilities
identifies callers via the `tool`/`email` params it documents separately
from Crossref's polite-pool convention, and this v1 adapter intentionally
sends neither (no hardcoded contact info, per project policy).
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from thaicite.adapters.base import AdapterError, RawRecord, SourceAdapter
from thaicite.core.models import Candidate, VerificationState

_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
_DEFAULT_TIMEOUT_S = 15
_NCBI_API_KEY_ENV = "THAICITE_NCBI_API_KEY"


class PubMedAdapter(SourceAdapter):
    name = "PUBMED"

    def __init__(self, timeout_s: float = _DEFAULT_TIMEOUT_S, retmax: int = 10) -> None:
        self.timeout_s = timeout_s
        self.retmax = retmax

    def _params(self, extra: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = dict(extra)
        api_key = os.environ.get(_NCBI_API_KEY_ENV)
        if api_key:
            params["api_key"] = api_key
        return params

    def _get(self, url: str, params: dict[str, Any]) -> requests.Response | AdapterError:
        try:
            return requests.get(url, params=params, timeout=self.timeout_s)
        except requests.exceptions.Timeout:
            return AdapterError(
                state=VerificationState.TIMEOUT,
                adapter=self.name,
                message=f"PubMed (E-Utilities) request timed out after {self.timeout_s}s.",
            )
        except requests.exceptions.RequestException as exc:
            # Connection errors, DNS failures, etc. -- not a "not found",
            # a genuine failure to reach the source at all.
            return AdapterError(
                state=VerificationState.TIMEOUT,
                adapter=self.name,
                message=f"PubMed (E-Utilities) request failed: {exc}",
            )

    def _map_http_error(self, response: requests.Response) -> AdapterError | None:
        if response.status_code == 429:
            return AdapterError(
                state=VerificationState.RATE_LIMITED,
                adapter=self.name,
                message="PubMed (E-Utilities) returned HTTP 429 (rate limited).",
            )
        if response.status_code in (401, 403):
            return AdapterError(
                state=VerificationState.ACCESS_DENIED,
                adapter=self.name,
                message=f"PubMed (E-Utilities) returned HTTP {response.status_code} "
                "(access denied).",
            )
        if 500 <= response.status_code < 600:
            return AdapterError(
                state=VerificationState.TIMEOUT,
                adapter=self.name,
                message=f"PubMed (E-Utilities) returned HTTP {response.status_code} "
                "(server error).",
            )
        if response.status_code != 200:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"PubMed (E-Utilities) returned unexpected HTTP "
                f"{response.status_code}.",
            )
        return None

    def search(self, query: str) -> list[RawRecord] | AdapterError:
        esearch_response = self._get(
            _ESEARCH_URL,
            self._params({"db": "pubmed", "term": query, "retmode": "json", "retmax": self.retmax}),
        )
        if isinstance(esearch_response, AdapterError):
            return esearch_response
        error = self._map_http_error(esearch_response)
        if error:
            return error

        try:
            esearch_payload = esearch_response.json()
        except ValueError as exc:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"PubMed ESearch response was not valid JSON: {exc}",
            )

        try:
            id_list = esearch_payload["esearchresult"]["idlist"]
        except (KeyError, TypeError) as exc:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"PubMed ESearch response missing expected "
                f"'esearchresult.idlist' field: {exc}",
            )

        if not id_list:
            return AdapterError(
                state=VerificationState.NOT_FOUND,
                adapter=self.name,
                message=f"PubMed ESearch returned zero PMIDs for query {query!r}.",
            )

        esummary_response = self._get(
            _ESUMMARY_URL,
            self._params({"db": "pubmed", "id": ",".join(id_list), "retmode": "json"}),
        )
        if isinstance(esummary_response, AdapterError):
            return esummary_response
        error = self._map_http_error(esummary_response)
        if error:
            return error

        try:
            esummary_payload = esummary_response.json()
        except ValueError as exc:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"PubMed ESummary response was not valid JSON: {exc}",
            )

        try:
            result = esummary_payload["result"]
            uids = result["uids"]
        except (KeyError, TypeError) as exc:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"PubMed ESummary response missing expected "
                f"'result.uids' field: {exc}",
            )

        retrieved_at = time.time()
        records: list[RawRecord] = []
        for uid in uids:
            item = result.get(uid)
            if not item or not isinstance(item, dict):
                # A uid with no summary body cannot become a traceable
                # Candidate; skip it rather than inventing metadata.
                continue
            records.append(
                RawRecord(source_record_id=uid, raw_metadata=item, retrieved_at=retrieved_at)
            )

        if not records:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message="PubMed ESummary returned uids but none had a usable summary body.",
            )

        return records

    def to_candidates(self, records: list[RawRecord]) -> list[Candidate]:
        """Convert real PubMed ESummary RawRecords into Candidate objects.

        Every field pulled here comes directly from NCBI's own JSON --
        nothing is inferred or invented by this adapter. A PMCID, when
        present in `articleids`, is surfaced on `Candidate.pmcid` for a
        later PMC full-text follow-up (out of scope here).
        """
        candidates: list[Candidate] = []
        for rec in records:
            meta: dict[str, Any] = rec.raw_metadata
            authors = [
                a.get("name", "") for a in (meta.get("authors") or []) if a.get("name")
            ]
            article_ids = meta.get("articleids") or []
            doi = None
            pmcid = None
            for aid in article_ids:
                idtype = aid.get("idtype")
                value = aid.get("value")
                if idtype == "doi" and value:
                    doi = value
                elif idtype == "pmc" and value:
                    pmcid = value

            pubdate = meta.get("pubdate") or ""
            year: int | None = None
            year_token = pubdate.split()[0] if pubdate else ""
            if year_token.isdigit():
                year = int(year_token)

            candidates.append(
                Candidate(
                    source_adapter=self.name,
                    source_record_id=rec.source_record_id,
                    title=meta.get("title") or "",
                    authors=authors,
                    year=year,
                    doi=doi,
                    pmid=rec.source_record_id,
                    pmcid=pmcid,
                    issn=meta.get("issn") or meta.get("essn"),
                    url=(
                        f"https://pubmed.ncbi.nlm.nih.gov/{rec.source_record_id}/"
                        if rec.source_record_id
                        else None
                    ),
                    abstract=None,  # ESummary does not carry abstract text.
                    raw_metadata=meta,
                    retrieved_at=rec.retrieved_at,
                )
            )
        return candidates
