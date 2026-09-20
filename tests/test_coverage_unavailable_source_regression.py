"""Regression: when one source FAILS (a real transport error) and another
source SUCCEEDS with zero matches, the final result must report the failed
source as UNAVAILABLE -- never silently dropped, and never collapsed into a
bare NOT_FOUND that reads identically to "everything was searched, nothing
matched".

This is the end-to-end composition of `core.engine.discover_citations()`
(or `resolve_citations()`) with `routing.router.route()`/`RouteDecision
.update_coverage()`, using two synthetic, fully-controlled offline adapters
(never live network) so the two source outcomes are pinned exactly:

  - adapter A ("OPENALEX" track): returns a real transport-error
    `AdapterError` (RATE_LIMITED) on every query -- a genuine failure.
  - adapter B ("CROSSREF" track): returns a NOT_FOUND `AdapterError` on
    every query -- a genuine, successful "zero matches" search, matching
    the real contract every adapter follows.

`RouteDecision.coverage` (after `update_coverage()`) must show A as
UNAVAILABLE with a reason and B as SEARCHED_OK -- not both folded into one
undifferentiated "not found", and A must never simply vanish from the
coverage readout.
"""

from __future__ import annotations

from thaicite.adapters.base import AdapterError
from thaicite.core import coverage as cov
from thaicite.core.engine import discover_citations, resolve_citations
from thaicite.core.models import VerificationState
from thaicite.routing.router import route


class _FailingAdapter:
    """Simulates a source that is genuinely UNREACHABLE this run -- a real
    transport-level error, never laundered into an empty/NOT_FOUND result
    (see adapters/base.py's own module docstring on this exact invariant)."""

    name = "OPENALEX"

    def search(self, query: str):
        return AdapterError(
            state=VerificationState.RATE_LIMITED,
            adapter=self.name,
            message="HTTP 429 -- rate limited by OpenAlex.",
        )

    def to_candidates(self, records):
        return []


class _EmptySuccessAdapter:
    """Simulates a source that WAS genuinely, successfully searched and
    simply found nothing -- a real, honest zero-match result, never an
    error laundered as such. Matches the actual contract every real
    adapter (openalex.py/crossref.py/pubmed.py/thaijo.py) follows: a
    genuine zero-match search returns a NOT_FOUND `AdapterError`, never a
    bare empty list -- see `adapters/base.py`'s own module docstring. A
    bare `[]` here would be unrealistic and would leave
    `RouteDecision.update_coverage()` with no evidence this adapter ever
    ran at all (see `core/coverage.py`'s `PLANNED` state)."""

    name = "CROSSREF"

    def search(self, query: str):
        return AdapterError(
            state=VerificationState.NOT_FOUND,
            adapter=self.name,
            message=f"No records returned for query {query!r}.",
        )

    def to_candidates(self, records):
        return []


def _route_and_run(context: str, query: str):
    adapters = [_FailingAdapter(), _EmptySuccessAdapter()]
    decision = route(context, query, adapters)
    result = discover_citations(context=context, adapters=decision.adapters)
    decision.update_track_status(result)
    decision.update_coverage(result)
    return decision, result


def test_failed_source_reported_unavailable_zero_match_source_reported_ok():
    context = "graph neural networks for traffic forecasting"
    decision, result = _route_and_run(context, context)

    assert result["candidates"] == []

    by_source = {e.source: e for e in decision.coverage}
    assert "OPENALEX" in by_source, "the failed source must not vanish from coverage"
    assert by_source["OPENALEX"].status == cov.UNAVAILABLE
    assert by_source["OPENALEX"].reason  # a reason must be present, not just a bare flag
    assert "RATE_LIMITED" in by_source["OPENALEX"].reason

    assert "CROSSREF" in by_source
    assert by_source["CROSSREF"].status == cov.SEARCHED_OK


def test_failed_source_track_status_reported_degraded_not_silently_ok():
    """The coarser global/local track health signal (`track_status`) must
    also reflect the real failure -- a healthy sibling on the SAME track
    (CROSSREF, also global) must never mask OPENALEX's real error."""
    context = "graph neural networks for traffic forecasting"
    decision, _result = _route_and_run(context, context)

    from thaicite.routing.router import STATUS_DEGRADED, TRACK_GLOBAL

    assert decision.track_status[TRACK_GLOBAL] == STATUS_DEGRADED


def test_failed_source_not_collapsed_into_bare_not_found_on_resolve_citations():
    """Same two-adapter shape driven through `resolve_citations()` (the
    identity-verification entry point) instead of `discover_citations()` --
    the coverage-level distinction must hold for both entry points, since
    `RouteDecision.update_coverage()`'s evidence-reading logic is shared."""
    claim = "This approach improves forecasting accuracy."
    adapters = [_FailingAdapter(), _EmptySuccessAdapter()]
    decision = route(claim, claim, adapters)
    result = resolve_citations(context=claim, queries=[claim], adapters=decision.adapters)
    decision.update_coverage(result)

    by_source = {e.source: e for e in decision.coverage}
    assert by_source["OPENALEX"].status == cov.UNAVAILABLE
    assert "RATE_LIMITED" in by_source["OPENALEX"].reason
    assert by_source["CROSSREF"].status == cov.SEARCHED_OK

    # The raw engine result itself still distinguishes the two sources in
    # its own rejected/by_adapter detail -- this is the evidence
    # update_coverage() reads from, confirmed directly so this test does
    # not depend on update_coverage() alone to prove the distinction exists.
    # Both adapters returned an `AdapterError` this query (OPENALEX a real
    # transport error, CROSSREF a genuine NOT_FOUND), so this lands in the
    # "rejected"/adapter_error bucket, not `not_found_queries` -- and
    # `by_adapter` must carry BOTH adapters' evidence, not just the one
    # that errored (round-4 fix, 2026-09-20 -- see core/engine.py).
    rejected_entry = result["rejected"].get(f"query::{claim}")
    assert rejected_entry is not None
    by_adapter = rejected_entry["by_adapter"]
    assert by_adapter["OPENALEX"]["state"] == VerificationState.RATE_LIMITED
    assert by_adapter["CROSSREF"]["state"] == VerificationState.NOT_FOUND


def test_coverage_is_not_all_negative_when_one_source_is_genuinely_ok():
    """`coverage_is_all_negative()` must be False here -- CROSSREF really
    was searched successfully, so this is NOT the "nothing was searchable
    at all" case, even though the result set is empty and one source did
    fail."""
    context = "graph neural networks for traffic forecasting"
    decision, _result = _route_and_run(context, context)
    assert cov.coverage_is_all_negative(decision.coverage) is False
