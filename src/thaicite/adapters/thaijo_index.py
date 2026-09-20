"""Local SQLite + FTS5 search index over harvested ThaiJO records.

Round 3 redesign (2026-09-20): OAI-PMH is a HARVESTING protocol, not a
search API (see `thaijo_harvester.py`'s module docstring for the full
rationale). This module is the READ side of that split: a local snapshot
database that `ThaiJOAdapter.search()` (in `thaijo.py`) queries instead of
hitting the network per query.

Schema: one FTS5 virtual table, `harvested_fts`. Rather than a normal table
plus an external-content FTS5 table kept in sync via triggers (correct, but
more moving parts than a v1 needs), this uses FTS5 as the single source of
truth, with the non-searchable fields carried as `UNINDEXED` columns on the
same row:

    endpoint            UNINDEXED  -- which harvested endpoint this came from
    native_id           UNINDEXED  -- the OAI-PMH <identifier>, e.g.
                                       "oai:tci-thaijo.org:article/1001"
    harvested_at        UNINDEXED  -- unix timestamp of the harvest that
                                       last (re-)wrote this row
    raw_metadata_json    UNINDEXED  -- the full per-record dict the harvester
                                       parsed from OAI-PMH Dublin Core
                                       (identifier/titles/creators/dates/
                                       identifiers/descriptions/source),
                                       serialized as JSON -- this is what
                                       `ThaiJOAdapter.to_candidates()`
                                       expects, so search() can rebuild an
                                       identical `RawRecord.raw_metadata`
                                       shape to what the old single-shot
                                       search() adapter used to hand it.
    title_tokens        (indexed)  -- title text, pre-tokenized through the
                                       SAME shared Thai-aware tokenizer used
                                       everywhere else in this codebase
                                       (`normalize.tokenize.tokenize()`),
                                       space-joined, and handed to FTS5 as
                                       already-segmented text. This is
                                       deliberate: FTS5's own built-in
                                       tokenizer has no Thai word-boundary
                                       knowledge (Thai script has no
                                       inter-word spaces), so if we let FTS5
                                       segment raw Thai text itself, a whole
                                       Thai title with no punctuation would
                                       become one giant token, exactly the
                                       bug `normalize/tokenize.py` exists to
                                       fix for the rest of this codebase. A
                                       custom FTS5 tokenizer hook would avoid
                                       the pre-join step, but the
                                       space-joined-pretokenized approach is
                                       simpler and explicitly acceptable for
                                       v1 (founder-approved redesign notes).
    abstract_tokens      (indexed)  -- same treatment for the
                                       abstract/description text.

Upsert semantics: `upsert_record()` is keyed on (endpoint, native_id) --
never a raw INSERT. Because FTS5 has no native UNIQUE constraint machinery
on UNINDEXED columns, an upsert here is done as an explicit
DELETE-then-INSERT inside one transaction: any prior row for that
(endpoint, native_id) is removed first, then the fresh row (with a current
`harvested_at`) is inserted. This never silently overwrites in a way that
loses provenance -- the record's (endpoint, native_id) identity and content
survive a re-sync unchanged except for `harvested_at` moving forward, and a
record harvested from one endpoint never collides with the same native_id
harvested from a different endpoint (the primary key is the pair, not the
native_id alone).

`search(query, limit=50)` builds a real FTS5 MATCH expression with
OR-semantics across the query's own tokens (tokenized through the same
shared tokenizer) -- deliberately not the old single-shot adapter's
AND-over-raw-split-tokens behavior, which required every query term to
appear verbatim in the haystack. Results are ranked by FTS5's own `bm25()`
relevance score (lower is more relevant in SQLite's bm25 convention, so
this orders ascending). Every hit carries its source endpoint and
`harvested_at` timestamp so a caller can see this is a snapshot readout of
a local database, not a live guarantee about what ThaiJO holds right now.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from thaicite.adapters.base import RawRecord
from thaicite.normalize.tokenize import tokenize

# Repo-root-relative default -- never outside the repo/user's own control.
# thaijo_index.py -> adapters -> thaicite -> src -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INDEX_DB_PATH = _REPO_ROOT / ".thaicite_cache" / "thaijo_index.db"

# Default "this snapshot is too old to call a fresh negative" window (14
# days). Not a claim about how often ThaiJO itself changes -- just the
# Coverage Readout's own honesty threshold: past this age, a "0 matches"
# result is reported as `UNAVAILABLE: stale index` rather than plain `OK`,
# so a caller never reads an old snapshot's silence as a live guarantee.
DEFAULT_STALE_THRESHOLD_S = 14 * 24 * 3600.0

_SCHEMA_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS harvested_fts USING fts5(
    endpoint UNINDEXED,
    native_id UNINDEXED,
    harvested_at UNINDEXED,
    raw_metadata_json UNINDEXED,
    title_tokens,
    abstract_tokens
);
"""

# Plain (non-FTS5) table: the last-known per-endpoint harvest STATUS
# (`thaijo_harvester.py::EndpointStatus`), independent of whether that
# endpoint contributed any records. Keyed on `code` (one row per category
# code, always overwritten -- this tracks the MOST RECENT sync attempt for
# that endpoint, not a history). This is what lets a query-time Coverage
# Readout (`ThaiJOAdapter.coverage()`) answer "was so01 ever reachable" even
# though `search()` itself never touches the network (round 3 redesign --
# see `thaijo_harvester.py`'s module docstring).
_ENDPOINT_STATUS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS endpoint_status (
    code TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    records_harvested INTEGER NOT NULL,
    synced_at REAL NOT NULL
);
"""


@dataclass
class IndexStats:
    """Honest snapshot metadata -- never presented as a live source count."""

    total_records: int
    endpoints: dict[str, int]
    oldest_harvested_at: float | None
    newest_harvested_at: float | None

    def is_stale(self, threshold_s: float = DEFAULT_STALE_THRESHOLD_S, now: float | None = None) -> bool:
        """True when the freshest record in this snapshot is older than
        `threshold_s`, OR when the snapshot has never been harvested at all
        (`newest_harvested_at is None`) -- an empty index is the maximally
        stale case, never reported as "fresh".
        """
        if self.newest_harvested_at is None:
            return True
        now = time.time() if now is None else now
        return (now - self.newest_harvested_at) > threshold_s


def _quote_fts_token(token: str) -> str:
    """Quote one token as an FTS5 string literal, escaping embedded quotes.

    FTS5 query syntax treats bare tokens containing certain punctuation as
    operators; quoting every token as a phrase literal (`"token"`) avoids
    that entirely and keeps this a plain OR-of-terms query, not a mini
    query-language surface.
    """
    return '"' + token.replace('"', '""') + '"'


class ThaiIndex:
    """A local SQLite+FTS5 snapshot of harvested ThaiJO records."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_INDEX_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute(_SCHEMA_SQL.strip())
        self._conn.execute(_ENDPOINT_STATUS_SCHEMA_SQL.strip())
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ThaiIndex":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- writes -----------------------------------------------------------

    def upsert_record(
        self,
        *,
        endpoint: str,
        native_id: str,
        title: str,
        abstract: str,
        raw_metadata: dict[str, Any],
        harvested_at: float | None = None,
    ) -> None:
        """Insert or refresh one record, keyed on (endpoint, native_id).

        `raw_metadata` should be the full per-record dict the harvester
        parsed (identifier/titles/creators/dates/identifiers/descriptions/
        source) so `search()` can hand back a `RawRecord.raw_metadata` in
        exactly the shape `ThaiJOAdapter.to_candidates()` already expects.
        """
        harvested_at = time.time() if harvested_at is None else harvested_at
        title_tokens = " ".join(tokenize(title or ""))
        abstract_tokens = " ".join(tokenize(abstract or ""))
        raw_metadata_json = json.dumps(raw_metadata)

        with self._conn:
            self._conn.execute(
                "DELETE FROM harvested_fts WHERE endpoint = ? AND native_id = ?",
                (endpoint, native_id),
            )
            self._conn.execute(
                "INSERT INTO harvested_fts "
                "(endpoint, native_id, harvested_at, raw_metadata_json, "
                "title_tokens, abstract_tokens) VALUES (?, ?, ?, ?, ?, ?)",
                (endpoint, native_id, harvested_at, raw_metadata_json, title_tokens, abstract_tokens),
            )

    # -- endpoint (harvest) coverage -----------------------------------

    def save_endpoint_status(
        self,
        *,
        code: str,
        url: str,
        status: str,
        records_harvested: int,
        synced_at: float | None = None,
    ) -> None:
        """Persist the MOST RECENT harvest attempt's status for one endpoint
        `code` (overwrites any prior row for the same code -- see
        `_ENDPOINT_STATUS_SCHEMA_SQL`'s docstring). Called by
        `thaijo_harvester.py::ThaiJOHarvester.sync()` once per endpoint,
        right after `EndpointStatus` is finalized for that endpoint.
        """
        synced_at = time.time() if synced_at is None else synced_at
        with self._conn:
            self._conn.execute(
                "INSERT INTO endpoint_status (code, url, status, records_harvested, synced_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(code) DO UPDATE SET "
                "url=excluded.url, status=excluded.status, "
                "records_harvested=excluded.records_harvested, synced_at=excluded.synced_at",
                (code, url, status, records_harvested, synced_at),
            )

    def get_endpoint_coverage(self) -> dict[str, dict[str, Any]]:
        """The last-known per-endpoint harvest status for every endpoint
        this index has ever recorded a sync attempt for, keyed by `code`.
        An endpoint with NO row here has never had a harvest attempt
        recorded at all (`ThaiJOAdapter.coverage()` reports those as
        NOT_ATTEMPTED, not as an error -- see that method).
        """
        rows = self._conn.execute(
            "SELECT code, url, status, records_harvested, synced_at FROM endpoint_status"
        ).fetchall()
        return {
            code: {
                "url": url,
                "status": status,
                "records_harvested": records_harvested,
                "synced_at": synced_at,
            }
            for code, url, status, records_harvested, synced_at in rows
        }

    # -- reads --------------------------------------------------------------

    def is_empty(self) -> bool:
        row = self._conn.execute("SELECT COUNT(*) FROM harvested_fts").fetchone()
        return bool(row is None or row[0] == 0)

    def stats(self) -> IndexStats:
        total_row = self._conn.execute("SELECT COUNT(*) FROM harvested_fts").fetchone()
        total = total_row[0] if total_row else 0

        endpoint_rows = self._conn.execute(
            "SELECT endpoint, COUNT(*) FROM harvested_fts GROUP BY endpoint"
        ).fetchall()
        endpoints = {row[0]: row[1] for row in endpoint_rows}

        minmax_row = self._conn.execute(
            "SELECT MIN(harvested_at), MAX(harvested_at) FROM harvested_fts"
        ).fetchone()
        oldest = minmax_row[0] if minmax_row else None
        newest = minmax_row[1] if minmax_row else None

        return IndexStats(
            total_records=total,
            endpoints=endpoints,
            oldest_harvested_at=oldest,
            newest_harvested_at=newest,
        )

    def search(self, query: str, limit: int = 50) -> list[RawRecord]:
        """Real FTS5 query with OR-semantics across the query's own tokens.

        Returns `RawRecord`s whose `raw_metadata` is exactly the harvested
        per-record dict (plus `_endpoint`/`_harvested_at` provenance keys),
        ranked by FTS5's own bm25() relevance score. An empty/whitespace
        query or a query with no tokenizable terms returns an empty list
        (never every row -- there is no "match everything" fallback here).
        """
        query_tokens = list(dict.fromkeys(tokenize(query)))  # de-dup, keep order
        if not query_tokens:
            return []

        match_expr = "title_tokens:(" + " OR ".join(_quote_fts_token(t) for t in query_tokens) + ")"
        match_expr += " OR abstract_tokens:(" + " OR ".join(_quote_fts_token(t) for t in query_tokens) + ")"

        rows = self._conn.execute(
            "SELECT endpoint, native_id, harvested_at, raw_metadata_json, "
            "bm25(harvested_fts) AS rank "
            "FROM harvested_fts WHERE harvested_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (match_expr, limit),
        ).fetchall()

        results: list[RawRecord] = []
        for endpoint, native_id, harvested_at, raw_metadata_json, _rank in rows:
            raw_metadata = json.loads(raw_metadata_json)
            # Provenance: this is a snapshot readout of a local database,
            # never a live guarantee about the source -- see module docstring.
            raw_metadata["_endpoint"] = endpoint
            raw_metadata["_harvested_at"] = harvested_at
            results.append(
                RawRecord(
                    source_record_id=native_id,
                    raw_metadata=raw_metadata,
                    retrieved_at=harvested_at,
                )
            )
        return results
