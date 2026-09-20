"""Scenario test harness: run_scenario(scenario) -> dict.

A scenario is a plain JSON-shaped dict:
    {
      "scenario_id": "...",
      "context": "...",
      "queries": [
         "a plain query string",
         {"text": "another query", "inject_error": "RATE_LIMITED"}
      ],
      "adapters": ["openalex"],
      "expect": { ... free-form, not interpreted here ... }
    }

`inject_error` on a query object simulates an adapter failure
(RATE_LIMITED / TIMEOUT / ACCESS_DENIED / PARSER_ERROR / NOT_FOUND) for
that ONE query, for adapters that don't currently support forcing a live
failure from OpenAlex on demand. It never fabricates a *Candidate* -- it
only ever fabricates a *failure*, which keeps the "AI never becomes the
source" invariant intact even under simulated-error testing.

This module only wires dependencies and packages output; it does not
re-derive or judge the `expect` block -- that is left to a separate judge
so the harness's own bugs cannot mark its own homework.
"""

from __future__ import annotations

from typing import Any

from thaicite.adapters.base import AdapterError, RawRecord, SourceAdapter
from thaicite.adapters.openalex import OpenAlexAdapter
from thaicite.core.engine import resolve_citations
from thaicite.core.models import VerificationState

_REAL_ADAPTER_FACTORIES = {
    "openalex": OpenAlexAdapter,
}

_VALID_INJECT_STATES = frozenset(
    {
        VerificationState.RATE_LIMITED,
        VerificationState.TIMEOUT,
        VerificationState.ACCESS_DENIED,
        VerificationState.PARSER_ERROR,
        VerificationState.NOT_FOUND,
    }
)


class _ErrorInjectingAdapter(SourceAdapter):
    """Wraps a real adapter; forces a tagged error for specific query text.

    Any query not in `injections` is passed through to the wrapped real
    adapter unchanged -- injection only ever affects the exact queries a
    scenario asked it to affect.
    """

    def __init__(self, real: SourceAdapter, injections: dict[str, str]) -> None:
        self._real = real
        self._injections = injections
        self.name = real.name

    def search(self, query: str) -> list[RawRecord] | AdapterError:
        injected_state = self._injections.get(query)
        if injected_state is not None:
            return AdapterError(
                state=injected_state,
                adapter=self._real.name,
                message=f"Simulated {injected_state} injected by test scenario for query {query!r}.",
                note="This failure was injected by the test harness, not returned by the real adapter."
                if injected_state == VerificationState.NOT_FOUND
                else None,
            )
        return self._real.search(query)

    def to_candidates(self, records):
        return self._real.to_candidates(records)


def _normalize_queries(raw_queries: list[Any]) -> tuple[list[str], dict[str, str]]:
    """Split scenario queries into (query_texts, {query_text: inject_state})."""
    texts: list[str] = []
    injections: dict[str, str] = {}
    for q in raw_queries:
        if isinstance(q, str):
            texts.append(q)
            continue
        if isinstance(q, dict):
            text = q.get("text") or q.get("query")
            if not text:
                raise ValueError(f"Scenario query object missing 'text'/'query': {q!r}")
            texts.append(text)
            inject = q.get("inject_error")
            if inject is not None:
                if inject not in _VALID_INJECT_STATES:
                    raise ValueError(
                        f"inject_error must be one of {sorted(_VALID_INJECT_STATES)}, got {inject!r}"
                    )
                injections[text] = inject
            continue
        raise ValueError(f"Unsupported query entry type: {type(q)!r}")
    return texts, injections


def _build_adapters(names: list[str], injections: dict[str, str]) -> list[SourceAdapter]:
    adapters: list[SourceAdapter] = []
    for name in names:
        factory = _REAL_ADAPTER_FACTORIES.get(name)
        if factory is None:
            raise ValueError(
                f"Unknown/unsupported adapter {name!r} for this prototype -- "
                f"supported: {sorted(_REAL_ADAPTER_FACTORIES)}"
            )
        real = factory()
        adapters.append(_ErrorInjectingAdapter(real, injections))
    return adapters


def run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """Run one scenario dict through the real engine; return actual + expect.

    Returns:
        {
          "scenario_id": ...,
          "actual": <raw resolve_citations() output, verified/rejected/not_found_queries>,
          "expect": <the scenario's own expect block, untouched>,
        }
    """
    scenario_id = scenario.get("scenario_id", "<unnamed>")
    context = scenario.get("context", "")
    raw_queries = scenario.get("queries", [])
    adapter_names = scenario.get("adapters", ["openalex"])
    expect = scenario.get("expect", {})

    query_texts, injections = _normalize_queries(raw_queries)
    adapters = _build_adapters(adapter_names, injections)

    actual = resolve_citations(context=context, queries=query_texts, adapters=adapters)

    return {
        "scenario_id": scenario_id,
        "actual": _serialize_result(actual),
        "expect": expect,
    }


def _serialize_result(result: dict[str, Any]) -> dict[str, Any]:
    """Convert Citation/CanonicalWork objects into plain JSON-friendly dicts."""
    verified = []
    for citation in result["verified"]:
        work = citation.work
        verified.append(
            {
                "title": work.primary.title,
                "state": work.state,
                "source_adapter": work.primary.source_adapter,
                "source_record_id": work.primary.source_record_id,
                "doi": work.primary.doi,
                "merged_identifiers": work.merged_identifiers,
                "num_candidates": len(work.candidates),
                "matched_keywords": citation.matched_keywords,
            }
        )
    return {
        "verified": verified,
        "rejected": result["rejected"],
        "not_found_queries": result["not_found_queries"],
    }
