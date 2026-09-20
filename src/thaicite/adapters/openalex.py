"""Real adapter for the OpenAlex Works API (https://api.openalex.org/works).

No authentication required. This is a REAL HTTP integration -- it is not
mocked, and a genuine transport failure must map to a distinct error state
(RATE_LIMITED / TIMEOUT / ACCESS_DENIED / PARSER_ERROR), never to NOT_FOUND.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from thaicite.adapters.base import AdapterError, RawRecord, SourceAdapter
from thaicite.core.models import Candidate, VerificationState

_SEARCH_URL = "https://api.openalex.org/works"
_DEFAULT_TIMEOUT_S = 15


class OpenAlexAdapter(SourceAdapter):
    name = "OPENALEX"

    def __init__(self, timeout_s: float = _DEFAULT_TIMEOUT_S, per_page: int = 10) -> None:
        self.timeout_s = timeout_s
        self.per_page = per_page

    def search(self, query: str) -> list[RawRecord] | AdapterError:
        try:
            response = requests.get(
                _SEARCH_URL,
                params={"search": query, "per_page": self.per_page},
                timeout=self.timeout_s,
            )
        except requests.exceptions.Timeout:
            return AdapterError(
                state=VerificationState.TIMEOUT,
                adapter=self.name,
                message=f"OpenAlex request timed out after {self.timeout_s}s.",
            )
        except requests.exceptions.RequestException as exc:
            # Connection errors, DNS failures, etc. -- not a "not found",
            # a genuine failure to reach the source at all.
            return AdapterError(
                state=VerificationState.TIMEOUT,
                adapter=self.name,
                message=f"OpenAlex request failed: {exc}",
            )

        if response.status_code == 429:
            return AdapterError(
                state=VerificationState.RATE_LIMITED,
                adapter=self.name,
                message="OpenAlex returned HTTP 429 (rate limited).",
            )
        if response.status_code in (401, 403):
            return AdapterError(
                state=VerificationState.ACCESS_DENIED,
                adapter=self.name,
                message=f"OpenAlex returned HTTP {response.status_code} (access denied).",
            )
        if 500 <= response.status_code < 600:
            return AdapterError(
                state=VerificationState.TIMEOUT,
                adapter=self.name,
                message=f"OpenAlex returned HTTP {response.status_code} (server error).",
            )
        if response.status_code != 200:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"OpenAlex returned unexpected HTTP {response.status_code}.",
            )

        try:
            payload = response.json()
        except ValueError as exc:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"OpenAlex response was not valid JSON: {exc}",
            )

        try:
            results = payload["results"]
        except (KeyError, TypeError) as exc:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message=f"OpenAlex response missing expected 'results' field: {exc}",
            )

        if not results:
            return AdapterError(
                state=VerificationState.NOT_FOUND,
                adapter=self.name,
                message=f"OpenAlex returned zero results for query {query!r}.",
            )

        retrieved_at = time.time()
        records: list[RawRecord] = []
        for item in results:
            work_id = item.get("id")
            if not work_id:
                # A result with no id cannot become a traceable Candidate;
                # skip it rather than inventing an id.
                continue
            records.append(
                RawRecord(source_record_id=work_id, raw_metadata=item, retrieved_at=retrieved_at)
            )

        if not records:
            return AdapterError(
                state=VerificationState.PARSER_ERROR,
                adapter=self.name,
                message="OpenAlex returned results but none had a usable work id.",
            )

        return records

    def to_candidates(self, records: list[RawRecord]) -> list[Candidate]:
        """Convert real OpenAlex RawRecords into Candidate objects.

        Every field pulled here comes directly from OpenAlex's own JSON --
        nothing is inferred or invented by this adapter.
        """
        candidates: list[Candidate] = []
        for rec in records:
            meta: dict[str, Any] = rec.raw_metadata
            authorships = meta.get("authorships") or []
            authors = [
                a.get("author", {}).get("display_name", "")
                for a in authorships
                if a.get("author", {}).get("display_name")
            ]
            doi = meta.get("doi")
            if doi:
                doi = doi.removeprefix("https://doi.org/")
            primary_location = meta.get("primary_location") or {}
            source = primary_location.get("source") or {}
            issn_list = source.get("issn") or []
            candidates.append(
                Candidate(
                    source_adapter=self.name,
                    source_record_id=rec.source_record_id,
                    title=meta.get("title") or meta.get("display_name") or "",
                    authors=authors,
                    year=meta.get("publication_year"),
                    doi=doi,
                    issn=issn_list[0] if issn_list else None,
                    url=primary_location.get("landing_page_url") or meta.get("id"),
                    abstract=_reconstruct_abstract(meta.get("abstract_inverted_index")),
                    raw_metadata=meta,
                    retrieved_at=rec.retrieved_at,
                )
            )
        return candidates


def _reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str | None:
    """OpenAlex serves abstracts as an inverted index; rebuild plain text.

    This reconstructs REAL text from REAL data OpenAlex returned -- it does
    not generate or guess an abstract when one is absent.
    """
    if not inverted_index:
        return None
    positions: dict[int, str] = {}
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions[i] = word
    if not positions:
        return None
    return " ".join(positions[i] for i in sorted(positions))
