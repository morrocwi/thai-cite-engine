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

Four states, per adapter AND per sub-endpoint where a source decomposes
into sub-endpoints (ThaiJO's per-category OAI-PMH endpoints):

  OK            -- searched successfully (even if 0 query-matching records).
  UNAVAILABLE   -- attempted, failed -- `reason` says why (RATE_LIMITED,
                   TIMEOUT, ACCESS_DENIED, PARSER_ERROR, or "index stale").
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

OK = "OK"
UNAVAILABLE = "UNAVAILABLE"
NOT_ATTEMPTED = "NOT_ATTEMPTED"
NOT_CONNECTED = "NOT_CONNECTED"

ALL_STATES = frozenset({OK, UNAVAILABLE, NOT_ATTEMPTED, NOT_CONNECTED})

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
    """True only if EVERY entry is UNAVAILABLE/NOT_ATTEMPTED/NOT_CONNECTED --
    i.e. nothing in the coverage list was ever actually, successfully
    searched. Used to decide whether a "not found" result needs the
    strongest possible caveat ("we did not really search anything") versus
    the ordinary "searched, found nothing" caveat.
    """
    return bool(entries) and all(e.status != OK for e in entries)
