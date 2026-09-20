"""Single entry point: context + queries + adapters -> verified citations.

`resolve_citations` is pure with respect to hallucinated data: every
Candidate it ever sees came from an adapter's `search()` result. If a
caller puts a fabricated title/DOI into `queries` and no adapter can find
it, that query MUST end up in `not_found_queries` (or `rejected`, if some
adapter returned something that then failed a gate) and MUST NOT appear in
`verified` under any circumstance -- there is no code path in this module
that invents a Candidate.
"""

from __future__ import annotations

from typing import Any

from thaicite.adapters.base import AdapterError, SourceAdapter
from thaicite.core.models import (
    Candidate,
    CiteUse,
    Citation,
    ContextContract,
    EvidenceLevel,
    VerificationState,
    freeze_context,
)
from thaicite.normalize.thai_relevance import classify_thai_relevance
from thaicite.resolve.conflicts import apply_conflict_state
from thaicite.resolve.identity import resolve_identities
from thaicite.evidence.relation import classify_relation
from thaicite.evidence.verifier import gate_admission_decision, verify


def resolve_citations(
    context: str,
    queries: list[str],
    adapters: list[SourceAdapter],
    context_contract: ContextContract | None = None,
) -> dict[str, Any]:
    """Resolve `queries` against `adapters`, returning verified citations.

    `context_contract` (ARCHITECTURE.md SS93) lets a caller freeze scope
    (claim, intended relation, population/geography/timeframe/language)
    BEFORE search. It is optional and purely additive for backward
    compatibility: when not supplied, one is built automatically from
    `context` (falling back to each `query` if `context` is empty), with
    `created_before_search=False` since it was not actually frozen ahead
    of this call by the caller.

    Returns:
        {
          "verified": list[Citation],
          "rejected": dict[str, dict]   # key: a stable label, value: {"reason", "state", "gate_results"?}
          "not_found_queries": dict[str, dict]  # query -> {"state", "note", "by_adapter"}
          "cite_uses": list[CiteUse]    # one CiteUse per (work, query) pair actually
                                         # evaluated -- see core/models.py::CiteUse
                                         # (ARCHITECTURE.md SS86/SS100). Additive: existing
                                         # callers reading only verified/rejected/
                                         # not_found_queries are unaffected.
        }
    """
    verified: list[Citation] = []
    rejected: dict[str, dict[str, Any]] = {}
    not_found_queries: dict[str, dict[str, Any]] = {}
    cite_uses: list[CiteUse] = []

    for query in queries:
        active_contract = context_contract or freeze_context(
            claim=context or query,
            created_before_search=False,
        )
        candidates: list[Candidate] = []
        errors_by_adapter: dict[str, AdapterError] = {}
        any_hit = False

        for adapter in adapters:
            result = adapter.search(query)
            if isinstance(result, AdapterError):
                errors_by_adapter[adapter.name] = result
                continue
            any_hit = True
            for candidate in adapter.to_candidates(result):
                # Classification/tagging only (never a gate) -- see
                # normalize/thai_relevance.py. Populated here so it flows
                # through to every downstream Candidate and, on VERIFIED,
                # to Citation.thai_relevance below.
                candidate.thai_relevance = classify_thai_relevance(candidate)
                candidates.append(candidate)

        if not candidates:
            # Nothing usable came back for this query from any adapter.
            # Distinguish a genuine "no results" from a real transport
            # failure: if every adapter reported a non-NOT_FOUND error
            # (rate limit/timeout/access denied/parser error), surface
            # that distinctly rather than folding it into NOT_FOUND.
            non_not_found = {
                name: err
                for name, err in errors_by_adapter.items()
                if err.state != VerificationState.NOT_FOUND
            }
            if non_not_found and not any_hit:
                rejected[f"query::{query}"] = {
                    "reason": "adapter_error",
                    "by_adapter": {
                        name: {"state": err.state, "message": err.message}
                        for name, err in non_not_found.items()
                    },
                }
            else:
                not_found_queries[query] = {
                    "state": VerificationState.NOT_FOUND,
                    "note": (
                        "No adapter returned a record for this query -- this "
                        "means not found via the adapters searched, not that "
                        "the work does not exist."
                    ),
                    "by_adapter": {
                        name: {"state": err.state, "message": err.message}
                        for name, err in errors_by_adapter.items()
                    },
                }
            continue

        # Identity resolution -- only real-identifier / bibliographic
        # merges happen here; can_merge() forbids semantic-only merging.
        works = resolve_identities(candidates)

        for work in works:
            apply_conflict_state(work)
            # `query` (the actual citation string being verified) drives the
            # REQUIRED G6 identity check; `context` only drives the
            # non-gating G6b informational signal. See evidence/verifier.py.
            work, matched_keywords = verify(work, context=context, query=query)

            # Citation-Use / Cite Card (ARCHITECTURE.md SS86/SS100): this
            # CanonicalWork evaluated against THIS specific claim/query --
            # a different query against the same work produces a separate
            # CiteUse, potentially with a different relation/decision.
            evidence_text = work.primary.abstract or ""
            evidence_level = (
                EvidenceLevel.ABSTRACT
                if evidence_text.strip()
                else EvidenceLevel.METADATA
            )
            relation = classify_relation(evidence_text, active_contract.claim)
            decision, decision_debug = gate_admission_decision(
                work, relation, intended_relation=active_contract.intended_relation
            )
            cite_uses.append(
                CiteUse(
                    claim=active_contract.claim,
                    work=work,
                    evidence_level=evidence_level,
                    relation=relation,
                    decision=decision,
                    gate_results=dict(work.gate_results),
                    decision_debug=decision_debug,
                    evidence_locator=(
                        "abstract" if evidence_level == EvidenceLevel.ABSTRACT else None
                    ),
                    query=query,
                    context=context,
                    matched_keywords=matched_keywords,
                    thai_relevance=work.primary.thai_relevance,
                )
            )

            if work.state == VerificationState.VERIFIED:
                verified.append(
                    Citation(
                        work=work,
                        context=context,
                        matched_keywords=matched_keywords,
                        thai_relevance=work.primary.thai_relevance,
                    )
                )
            else:
                label = (
                    f"{work.primary.source_adapter}:{work.primary.source_record_id}"
                )
                rejected[label] = {
                    "reason": _rejection_reason(work),
                    "state": work.state,
                    "gate_results": work.gate_results,
                    "conflicts": work.conflicts,
                    "title": work.primary.title,
                    "query": query,
                }

    return {
        "verified": verified,
        "rejected": rejected,
        "not_found_queries": not_found_queries,
        "cite_uses": cite_uses,
    }


def _rejection_reason(work) -> str:
    if work.state == VerificationState.CONFLICT:
        fields = ", ".join(c["field"] for c in work.conflicts)
        return f"METADATA_CONFLICT on field(s): {fields}"
    failed_gates = [name for name, passed in work.gate_results.items() if not passed]
    if failed_gates:
        return f"failed gate(s): {', '.join(failed_gates)}"
    return "rejected"
