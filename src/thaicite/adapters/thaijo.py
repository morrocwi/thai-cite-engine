"""Thin adapter for ThaiJO (Thai Journals Online), backed by a local index.

ROUND 3 REDESIGN (2026-09-20, founder-approved): OAI-PMH is a HARVESTING
protocol, not a search API -- treating it as "search-per-query" (round 2's
approach, previously implemented directly in this file) was structurally
wrong. See `thaijo_harvester.py`'s module docstring for the full incident
writeup (repeated re-harvesting per query variant, a fixed shared budget
that starved later/likely-more-relevant endpoints, and endpoint URLs that
were never actually verified -- confirmed this session: the pre-round-3
code AND its own test suite both assumed the wrong, path-based endpoint
convention; ThaiJO's real convention is subdomain-based, see
`thaijo_harvester.py::_endpoint_url`).

This module is now the THIN wrapper side of a Harvester + Local Index
split:

  * `thaijo_harvester.py::ThaiJOHarvester.sync()` -- the network-touching,
    OAI-PMH-following, per-endpoint harvest. Run this FIRST, as an
    explicit operator step (`python -m thaicite.adapters.thaijo_harvester
    --sync`), before relying on `ThaiJOAdapter.search()` below.
  * `thaijo_index.py::ThaiIndex` -- the local SQLite+FTS5 snapshot the
    harvester writes into and this adapter reads from.
  * `ThaiJOAdapter` (this class) -- kept under this name/interface for
    compatibility with `SourceAdapter` and every existing caller
    (`cli.py`, `routing/router.py`, etc.). `search(query)` now runs an
    INSTANT local FTS5 query against `self._index` -- no network call, no
    per-query re-harvesting -- and `to_candidates()` is UNCHANGED from
    round 2: it still reads `titles`/`creators`/`dates`/`identifiers`/
    `descriptions` out of `RawRecord.raw_metadata`, which is exactly the
    shape `ThaiIndex.search()` reconstructs from what the harvester stored
    (see `thaijo_index.py`'s module docstring).

If the local index has never been synced (empty/missing database),
`search()` returns an explicit `AdapterError` whose `note` says plainly
that a harvest/sync is needed first -- this is NEVER the same claim as "no
matching records exist at the source"; see the note text below and
`docs/ARCHITECTURE_NOTE.md` / `README.md` for the documented harvest step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from thaicite.adapters.base import AdapterError, RawRecord, SourceAdapter
from thaicite.adapters.thaijo_harvester import default_endpoint_codes
from thaicite.adapters.thaijo_index import DEFAULT_STALE_THRESHOLD_S, ThaiIndex
from thaicite.core import coverage as cov
from thaicite.core.models import Candidate, VerificationState

_DEFAULT_SEARCH_LIMIT = 50


class ThaiJOAdapter(SourceAdapter):
    name = "THAIJO"

    def __init__(
        self,
        index_db_path: str | Path | None = None,
        index: ThaiIndex | None = None,
        limit: int = _DEFAULT_SEARCH_LIMIT,
    ) -> None:
        """`index_db_path` (default: `<repo>/.thaicite_cache/thaijo_index.db`,
        see `thaijo_index.DEFAULT_INDEX_DB_PATH`) is always configurable and
        never hardcoded outside the repo/user's control. Pass `index=` directly
        (e.g. an in-memory/temp `ThaiIndex` in tests) to bypass path handling
        entirely.
        """
        self._index = index if index is not None else ThaiIndex(index_db_path)
        self.limit = limit

    def search(self, query: str) -> list[RawRecord] | AdapterError:
        if self._index.is_empty():
            return AdapterError(
                state=VerificationState.NOT_FOUND,
                adapter=self.name,
                message=(
                    f"Local ThaiJO index at {self._index.db_path} has never been "
                    "synced (0 records) -- a harvest is needed before this adapter "
                    "can search ThaiJO."
                ),
                note=(
                    "This is NOT a source-level 'no matching records exist' "
                    "signal -- the local snapshot is simply empty. Run "
                    "`python -m thaicite.adapters.thaijo_harvester --sync` "
                    "(see that module's docstring / README.md) before relying "
                    "on ThaiJO search results."
                ),
                coverage=self.coverage_summary(),
            )

        records = self._index.search(query, limit=self.limit)
        if not records:
            stats = self._index.stats()
            is_stale = stats.is_stale()
            return AdapterError(
                state=VerificationState.NOT_FOUND,
                adapter=self.name,
                message=(
                    f"Local ThaiJO index snapshot returned no records matching "
                    f"query {query!r}."
                ),
                note=(
                    (
                        "This snapshot HAS records but was last harvested "
                        "over the staleness threshold ago -- see `coverage` "
                        "for exactly how old. This is 'searched, nothing "
                        "matched, against a STALE snapshot', not a live "
                        "guarantee about what ThaiJO currently holds."
                    )
                    if is_stale
                    else (
                        "This reflects the local snapshot only (see "
                        "`thaijo_harvester.py --sync` to refresh it) -- not a "
                        "live guarantee about what ThaiJO currently holds. "
                        "'Searched, nothing matched' -- see `coverage` for "
                        "which endpoints actually contributed this snapshot."
                    )
                ),
                coverage=self.coverage_summary(),
            )
        return records

    # -- Coverage Readout (core/coverage.py) --------------------------------

    def coverage_summary(self) -> dict[str, Any]:
        """Structured Coverage Readout for THIS adapter -- index-level
        staleness/emptiness plus a per-endpoint (per-category-code)
        SEARCHED_OK/UNAVAILABLE/NOT_ATTEMPTED breakdown, so a caller can
        distinguish "genuinely searched a fresh snapshot, nothing matched"
        from "this source was never actually searched" and from "searched,
        but against a stale snapshot" (see `core/coverage.py`'s module
        docstring and this project's Coverage Readout redesign).

        `search()` attaches exactly this (via `coverage_summary()`) to every
        NOT_FOUND `AdapterError` it returns -- `RouteDecision.update_coverage()`
        reads `index_never_synced`/`index_is_stale` straight out of this dict
        (via `AdapterError.coverage`) to resolve the a-priori `PLANNED` entry
        into `UNAVAILABLE`/`STALE`/`SEARCHED_OK`; `coverage_entries()` below
        turns this same snapshot into typed `CoverageEntry` rows for
        `routing/router.py`.
        """
        stats = self._index.stats()
        endpoint_status = self._index.get_endpoint_coverage()
        return {
            "index_db_path": str(self._index.db_path),
            "index_total_records": stats.total_records,
            "index_never_synced": stats.total_records == 0,
            "index_oldest_harvested_at": stats.oldest_harvested_at,
            "index_newest_harvested_at": stats.newest_harvested_at,
            "index_is_stale": stats.is_stale(),
            "stale_threshold_s": DEFAULT_STALE_THRESHOLD_S,
            "endpoints": endpoint_status,
        }

    def coverage_entries(self) -> list[cov.CoverageEntry]:
        """One `CoverageEntry` per default ThaiJO category-code endpoint
        (`thaijo_harvester.default_endpoint_codes()`), built from the last
        persisted `endpoint_status` row for each code -- an endpoint with NO
        recorded sync attempt is NOT_ATTEMPTED, never silently omitted.

        These rows report HARVEST status (was this endpoint ever reachable
        by `ThaiJOHarvester.sync()`), which already happened in the past --
        never the a-priori `PLANNED` state `routing/router.py::route()`
        uses for "will be attempted this call" (that a-priori state is
        never appropriate here since harvesting is a separate, already-
        completed operator step; see this module's docstring). A completed,
        successful harvest is reported `SEARCHED_OK` (real, confirmed
        evidence this endpoint's content is in the local snapshot), a
        failed harvest `UNAVAILABLE`.
        """
        status_by_code = self._index.get_endpoint_coverage()
        entries: list[cov.CoverageEntry] = []
        for code in default_endpoint_codes():
            row = status_by_code.get(code)
            if row is None:
                entries.append(
                    cov.CoverageEntry(
                        source=f"{self.name}:{code}",
                        status=cov.NOT_ATTEMPTED,
                        reason="no harvest has ever been recorded for this endpoint",
                    )
                )
                continue
            raw_status = row["status"]
            if raw_status == "OK":
                entries.append(
                    cov.CoverageEntry(
                        source=f"{self.name}:{code}",
                        status=cov.SEARCHED_OK,
                        reason=f"{row['records_harvested']} record(s) harvested",
                        detail=row,
                    )
                )
            else:
                # "UNAVAILABLE:<state>:<message>" from EndpointStatus.mark_unavailable().
                entries.append(
                    cov.CoverageEntry(
                        source=f"{self.name}:{code}",
                        status=cov.UNAVAILABLE,
                        reason=raw_status.split(":", 1)[-1] if ":" in raw_status else raw_status,
                        detail=row,
                    )
                )
        return entries

    def to_candidates(self, records: list[RawRecord]) -> list[Candidate]:
        """Convert ThaiIndex-sourced RawRecords into Candidate objects.

        Unchanged from round 2: every field pulled here comes directly from
        the harvested OAI-PMH Dublin Core data stored in `raw_metadata` --
        nothing is inferred or invented by this adapter.
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
