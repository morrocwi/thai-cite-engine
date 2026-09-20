"""Coverage Readout -- honest "not found, GIVEN this coverage" reporting.

Founder-approved redesign principle (2026-09-20): "ไม่พบงาน" (nothing found)
must always be reported together with "ภายใต้แหล่งที่ค้นได้เหล่านี้" (given
which sources were actually searchable) -- never a bare "not found" that
silently hides a source-availability problem.

CONFIRMED BUG this module fixes: the pre-round-3 ThaiJO adapter could let
one endpoint succeed (even with zero query-matching records) while OTHER
endpoints genuinely failed (404/timeout/rate-limited), and the overall
result could still present as a plain NOT_FOUND with the other endpoints'
real failures silently dropped -- violating this project's own invariant
that NOT_FOUND must never be conflated with a source being unavailable
(`adapters/base.py`'s module docstring). This module gives every caller
(`routing/router.py::RouteDecision`, `cli.py`, `mcp_server.py`) ONE shared,
typed vocabulary for "how much of the source landscape did we actually get
to look at", independent of whether anything was found.

CONFIRMED BUG (round 4, 2026-09-20, this fix): the original four-state
vocabulary (OK/UNAVAILABLE/NOT_ATTEMPTED/NOT_CONNECTED) conflated two very
different meanings under one `OK` label -- "this adapter is queued to be
searched" (an a-priori intention, set by `routing/router.py::route()`
BEFORE any search ran) and "this adapter was actually, successfully,
freshly searched" (a post-search fact). Two concrete failures fell out of
that conflation:

  1. `ThaiJOAdapter.search()` could know its own local snapshot was stale
     (`coverage_summary()["index_is_stale"] is True`) while still returning
     a bare NOT_FOUND `AdapterError` -- and because NOT_FOUND was never one
     of the states `RouteDecision.update_coverage()` treated as "downgrade
     away from OK" (only RATE_LIMITED/TIMEOUT/ACCESS_DENIED/PARSER_ERROR
     were), a 3-month-old, never-re-synced snapshot returning zero matches
     could present to a caller as `THAIJO:so01 = OK`, indistinguishable
     from a genuinely fresh, successful search.
  2. `route()`'s a-priori `OK`, set for every adapter it plans to include
     before any search has run, was never actually confirmed by real
     post-search evidence for the (very common) case of "adapter ran,
     found nothing, reported a plain NOT_FOUND" -- that adapter's `OK` was
     simply left untouched, so a caller could not tell "we truly searched
     this and it came back empty" apart from "we never got any evidence
     this adapter ran at all this call".

The fix: split the old `OK` into `PLANNED` (the a-priori intention) and
`SEARCHED_OK` (a confirmed, executed, fresh search), and add `STALE` as its
own state (executed against a snapshot past its staleness threshold --
distinct from both a fresh success and a hard transport failure).

Six states, per adapter AND per sub-endpoint where a source decomposes into
sub-endpoints (ThaiJO's per-category OAI-PMH endpoints):

  PLANNED       -- `route()` included this adapter for this (context, query)
                   but no search has run yet. The new a-priori default,
                   replacing the old a-priori OK. A `PLANNED` entry that
                   survives all the way to a printed/returned result means
                   no real post-search evidence was ever folded back in for
                   that adapter -- itself worth surfacing, never silently
                   treated as a success.
  SEARCHED_OK   -- genuinely executed and returned real, fresh-enough
                   results (or a confirmed fresh empty result).
  STALE         -- executed against a snapshot past its staleness
                   threshold -- distinct from a hard failure: the search
                   itself ran, but its answer reflects old data, not a live
                   guarantee.
  UNAVAILABLE   -- attempted, failed -- `reason` says why (RATE_LIMITED,
                   TIMEOUT, ACCESS_DENIED, PARSER_ERROR, an index that has
                   never been synced at all, or "index stale" for a
                   sub-endpoint-level detail).
  NOT_ATTEMPTED -- never queried this run (e.g. a budget/ordering/domain-
                   routing decision excluded it -- `routing/router.py`'s
                   HEALTH domain excluding ThaiJO is a real example).
  NOT_CONNECTED -- the adapter/source is not configured at all (e.g. TNRR/
                   TCI, which are out of this project's v1 scope --
                   ARCHITECTURE.md SS56).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PLANNED = "PLANNED"
SEARCHED_OK = "SEARCHED_OK"
STALE = "STALE"
UNAVAILABLE = "UNAVAILABLE"
NOT_ATTEMPTED = "NOT_ATTEMPTED"
NOT_CONNECTED = "NOT_CONNECTED"

ALL_STATES = frozenset(
    {PLANNED, SEARCHED_OK, STALE, UNAVAILABLE, NOT_ATTEMPTED, NOT_CONNECTED}
)

# The only state that represents a CONFIRMED, fresh, successfully-executed
# search -- everything else (including PLANNED and STALE) is either "not
# actually confirmed yet" or "confirmed but with a real caveat attached".
CONFIRMED_FRESH_STATES = frozenset({SEARCHED_OK})

# Sources this project knows about but implements no adapter for at all in
# v1 (ARCHITECTURE.md SS56) -- always reported NOT_CONNECTED, regardless of
# domain/routing, so a coverage readout never silently omits them the way a
# bare "not found" would.
KNOWN_UNCONFIGURED_SOURCES: tuple[str, ...] = ("TNRR", "TCI")


@dataclass
class CoverageEntry:
    """One row of the coverage readout -- one adapter, or one adapter's
    sub-endpoint (e.g. `source="THAIJO:so01"`).
    """

    source: str
    status: str
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in ALL_STATES:
            raise ValueError(
                f"CoverageEntry.status must be one of {sorted(ALL_STATES)}, "
                f"got {self.status!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
        }


def format_coverage_lines(entries: list[CoverageEntry]) -> list[str]:
    """Plain-text lines for CLI display -- one per `CoverageEntry`."""
    lines = []
    for e in entries:
        reason = f" -- {e.reason}" if e.reason else ""
        lines.append(f"  {e.source:20s} {e.status}{reason}")
    return lines


def coverage_is_all_negative(entries: list[CoverageEntry]) -> bool:
    """True only if NO entry ever reached a confirmed, fresh `SEARCHED_OK`
    -- i.e. nothing in the coverage list was ever actually, successfully,
    freshly searched. `PLANNED` (never confirmed either way) and `STALE`
    (executed, but against out-of-date data) both count as negative here,
    same as `UNAVAILABLE`/`NOT_ATTEMPTED`/`NOT_CONNECTED` -- none of them is
    a confirmed fresh search. Used to decide whether a "not found" result
    needs the strongest possible caveat ("we did not really search anything
    we can currently vouch for") versus the ordinary "searched, found
    nothing" caveat.
    """
    return bool(entries) and all(e.status not in CONFIRMED_FRESH_STATES for e in entries)


def coverage_has_stale(entries: list[CoverageEntry]) -> bool:
    """True when at least one entry is `STALE` -- used to give a "this
    reflects an old snapshot, not a live guarantee" caveat its own,
    distinct wording from the harsher "nothing was ever searchable at all"
    caveat `coverage_is_all_negative()` guards.
    """
    return any(e.status == STALE for e in entries)


def coverage_has_unconfirmed_planned(entries: list[CoverageEntry]) -> bool:
    """True when at least one entry is still `PLANNED` -- meaning `route()`
    intended to search that adapter but no real post-search evidence (a
    result, a NOT_FOUND, or a transport error) was ever folded back into
    the coverage readout for it. This should be rare/never in a normal
    end-to-end call (every routed adapter is actually invoked), but a
    caller that never called `RouteDecision.update_coverage()` at all, or
    an adapter that raised instead of returning `AdapterError`, would leave
    entries in this state -- worth its own honest flag rather than silently
    reading as either success or failure.
    """
    return any(e.status == PLANNED for e in entries)
