"""Real adapter for the Crossref REST API (https://api.crossref.org/works).

No authentication required. This is a REAL HTTP integration -- it is not
mocked, and a genuine transport failure must map to a distinct error state
(RATE_LIMITED / TIMEOUT / ACCESS_DENIED / PARSER_ERROR), never to NOT_FOUND.

Polite-pool contact email: if the optional `THAICITE_CONTACT_EMAIL`
environment variable is set, its value is sent as Crossref's own documented
`mailto` query parameter (per Crossref's "polite pool" convention). If unset,
no contact email is sent at all -- there is no hardcoded fallback address.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from thaicite.adapters.base import AdapterError, RawRecord, SourceAdapter
from thaicite.core.models import Candidate, VerificationState

_SEARCH_URL = "https://api.crossref.org/works"
_DEFAULT_TIMEOUT_S = 15
_CONTACT_EMAIL_ENV = "THAICITE_CONTACT_EMAIL"


class CrossrefAdapter(SourceAdapter):
    name = "CROSSREF"

    def __init__(self, timeout_s: float = _DEFAULT_TIMEOUT_S, rows: int = 10) -> None:
        self.timeout_s = timeout_s
        self.rows = rows

    def search(self, query: str) -> list[RawRecord] | AdapterError:
        params: dict[str, Any] = {"query": query, "rows": self.rows}
        contact_email = os.environ.get(_CONTACT_EMAIL_ENV)
        if contact_email:
            params["mailto"] = contact_email

        try:
            response = requests.get(_SEARCH_URL, params=params, timeout=self.timeout_s)
        except requests.exceptions.Timeout:
            return AdapterError(
                state=VerificationState.TIMEOUT,
                adapter=self.name,
                message=f"Crossref request timed out after {self.timeout_s}s.",
            )
        except requests.exceptions.RequestException as exc:
            # Connection errors, DNS failures, etc. -- not a "not found",
            # a genuine failure to reach the source at all.
            return AdapterError(
                state=VerificationState.TIMEOUT,
                adapter=self.name,
                message=f"Crossref request failed: {exc}",
            )

        if response.status_code == 429:
            return AdapterError(
                state=VerificationState.RATE_LIMITED,
                adapter=self.name,
                message="Crossref returned HTTP 429 (rate limited).",
            )
        if response.status_code in (401, 403):
            return AdapterError(
                state=VerificationState.ACCESS_DENIED,
                adapter=self.name,
                message=f"Crossref returned HTTP {response.status_code} (access denied).",
            )
        if 500 <= response.status_code < 600:
            return AdapterError(
                state=VerificationState.TIMEOUT,
                adapter=self.name,
                message=f"Crossref returned HTTP {response.status_code} (server error).",
            )
        if response.status_code != 200:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"Crossref returned unexpected HTTP {response.status_code}.",
            )

        try:
            payload = response.json()
        except ValueError as exc:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"Crossref response was not valid JSON: {exc}",
            )

        try:
            items = payload["message"]["items"]
        except (KeyError, TypeError) as exc:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"Crossref response missing expected 'message.items' field: {exc}",
            )

        if not items:
            return AdapterError(
                state=VerificationState.NOT_FOUND,
                adapter=self.name,
                message=f"Crossref returned zero results for query {query!r}.",
            )

        retrieved_at = time.time()
        records: list[RawRecord] = []
        for item in items:
            doi = item.get("DOI")
            if not doi:
                # A result with no DOI cannot become a traceable Candidate
                # via this adapter; skip it rather than inventing an id.
                continue
            records.append(
                RawRecord(source_record_id=doi, raw_metadata=item, retrieved_at=retrieved_at)
            )

        if not records:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message="Crossref returned results but none had a usable DOI.",
            )

        return records

    def to_candidates(self, records: list[RawRecord]) -> list[Candidate]:
        """Convert real Crossref RawRecords into Candidate objects.

        Every field pulled here comes directly from Crossref's own JSON --
        nothing is inferred or invented by this adapter.
        """
        candidates: list[Candidate] = []
        for rec in records:
            meta: dict[str, Any] = rec.raw_metadata
            authors = [
                " ".join(part for part in (a.get("given"), a.get("family")) if part)
                for a in (meta.get("author") or [])
                if a.get("family") or a.get("given")
            ]
            titles = meta.get("title") or []
            title = titles[0] if titles else ""
            issued = meta.get("issued") or {}
            date_parts = issued.get("date-parts") or []
            year = None
            if date_parts and date_parts[0]:
                year = date_parts[0][0]
            issn_list = meta.get("ISSN") or []
            candidates.append(
                Candidate(
                    source_adapter=self.name,
                    source_record_id=rec.source_record_id,
                    title=title,
                    authors=authors,
                    year=year,
                    doi=meta.get("DOI"),
                    issn=issn_list[0] if issn_list else None,
                    url=meta.get("URL"),
                    abstract=meta.get("abstract"),
                    raw_metadata=meta,
                    retrieved_at=rec.retrieved_at,
                )
            )
        return candidates
