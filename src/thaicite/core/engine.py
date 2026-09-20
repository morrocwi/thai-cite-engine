"""Single entry point: context + queries + adapters -> verified citations.

`resolve_citations` is pure with respect to hallucinated data: every
Candidate it ever sees came from an adapter's `search()` result. If a
caller puts a fabricated title/DOI into `queries` and no adapter can find
it, that query MUST end up in `not_found_queries` (or `rejected`, if some
adapter returned something that then failed a gate) and MUST NOT appear in
`verified` under any circumstance -- there is no code path in this module
that invents a Candidate.

**`verified` is the public "safe to cite" list, gated on `CiteUse.decision
== Decision.ADMIT` (2026-09-20 fix).** `work.state == VerificationState
.VERIFIED` only proves the G1-G7 identity/existence gates passed -- it says
nothing about whether the evidence actually supports the claim it was
queried against. Before this fix, `verified` was built straight from
`work.state == VerificationState.VERIFIED`, so a work whose CiteUse.decision
came back HOLD (e.g. UNCLEAR/CONTEXT_ONLY relation, an unresolved conflict)
or REJECT (a real directional mismatch) could still land in the list a
caller treats as "safe to cite" (mcp_server.py's `verify_cite()` could
return `{"verified": true, "decision": "HOLD"}` simultaneously). A work that
reaches VERIFIED but whose CiteUse decision is HOLD or REJECT now never
enters `verified`: it is still fully discoverable via `cite_uses` (every
evaluated (work, query) pair, decision included) and, for a quick caller
that doesn't want to scan `cite_uses`, via the new `held` bucket (decision
== HOLD) or `rejected` (decision == REJECT, merged in alongside the
G1-G7-failure rejections, distinguishable by `reason` starting with
`admission_`).

**Two entry points, two different questions (2026-09-20, role-collision
fix):** `resolve_citations()` (below) answers "is THIS candidate the SAME
work as THIS citation string?" -- identity-verification mode, `verify_cite()`'s
job, and its behavior/signature/return shape are UNCHANGED by this fix.
`discover_citations()` (further down this module) answers "what real,
relevant work exists for this broad topic?" -- discovery mode, `find_cites()`'s
job. Before this fix both `find_cites()` and the CLI `find` command called
`resolve_citations(context=context, queries=[context], ...)`, which fed the
raw free-text discovery context into G6's REQUIRED strict candidate-vs-query
identity check (`evidence/verifier.py::gate_g6_identity_match`) -- a check
designed for "is this the exact work being cited", not "is this on-topic" --
so a real, relevant paper was rejected whenever its title did not share 2+
literal tokens with a topic phrase that was never meant to BE a citation
string. See `discover_citations()`'s own docstring for the fix.
"""

from __future__ import annotations

from typing import Any

from thaicite.adapters.base import AdapterError, SourceAdapter
from thaicite.core.models import (
    Candidate,
    CiteUse,
    Citation,
    ContextContract,
    Decision,
    EvidenceLevel,
    VerificationState,
    freeze_context,
)
from thaicite.normalize.thai_relevance import classify_thai_relevance
from thaicite.resolve.conflicts import apply_conflict_state
from thaicite.resolve.identity import resolve_identities
from thaicite.evidence.relation import classify_relation
from thaicite.evidence.verifier import (
    VERIFY_MODE_DISCOVERY,
    VERIFY_MODE_IDENTITY,
    gate_admission_decision,
    verify,
)
from thaicite.routing.query_planner import plan_queries


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
          "verified": list[Citation]    # ONLY works whose matching CiteUse.decision
                                         # == Decision.ADMIT -- the public "safe to
                                         # cite" list (see module docstring).
          "rejected": dict[str, dict]   # key: a stable label, value: {"reason", "state", "gate_results"?}
                                         # includes both G1-G7 gate-failure rejections
                                         # AND VERIFIED-but-decision==REJECT admission
                                         # rejections (reason prefixed "admission_reject:").
          "held": dict[str, dict]       # VERIFIED-but-decision==HOLD works: identity
                                         # confirmed, but not (yet) admissible as a
                                         # citation for this claim -- never mixed into
                                         # `verified`. Same value shape as `rejected`.
          "not_found_queries": dict[str, dict]  # query -> {"state", "note", "by_adapter"}
          "cite_uses": list[CiteUse]    # one CiteUse per (work, query) pair actually
                                         # evaluated -- see core/models.py::CiteUse
                                         # (ARCHITECTURE.md SS86/SS100). This is the
                                         # authoritative record of every decision made,
                                         # ADMIT/REJECT/HOLD alike.
        }
    """
    verified: list[Citation] = []
    rejected: dict[str, dict[str, Any]] = {}
    held: dict[str, dict[str, Any]] = {}
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
            _evaluate_work(
                work,
                context=context,
                query=query,
                active_contract=active_contract,
                mode=VERIFY_MODE_IDENTITY,
                verified=verified,
                rejected=rejected,
                held=held,
                cite_uses=cite_uses,
            )

    return {
        "verified": verified,
        "rejected": rejected,
        "held": held,
        "not_found_queries": not_found_queries,
        "cite_uses": cite_uses,
    }


def _evaluate_work(
    work,
    *,
    context: str,
    query: str,
    active_contract: ContextContract,
    mode: str,
    verified: list[Citation],
    rejected: dict[str, dict[str, Any]],
    held: dict[str, dict[str, Any]],
    cite_uses: list[CiteUse],
) -> None:
    """Run G1-G7 + the 3-way admission gate for ONE `work` and file the
    result into the caller's `verified`/`rejected`/`held`/`cite_uses`
    accumulators.

    Shared by `resolve_citations()` (identity mode, one CiteUse per
    (work, query) actually searched) and `discover_citations()` (discovery
    mode, one CiteUse per work after fusing/deduping the whole query
    family -- see that function's docstring). `mode` picks which G6
    variant gates `work.state` inside `verify()`/`gate_admission_decision()`
    (see evidence/verifier.py); every other gate and the ADMIT/REJECT/HOLD
    semantics are identical in both modes.
    """
    apply_conflict_state(work)
    # `query` (the actual citation string being verified, in identity mode)
    # drives the REQUIRED G6 identity check; `context` drives the
    # non-gating G6b informational signal in identity mode, and the
    # REQUIRED G6 discovery-relevance check in discovery mode. See
    # evidence/verifier.py.
    work, matched_keywords = verify(work, context=context, query=query, mode=mode)

    # Citation-Use / Cite Card (ARCHITECTURE.md SS86/SS100): this
    # CanonicalWork evaluated against THIS specific claim/query -- a
    # different query against the same work produces a separate CiteUse,
    # potentially with a different relation/decision.
    evidence_text = work.primary.abstract or ""
    evidence_level = (
        EvidenceLevel.ABSTRACT if evidence_text.strip() else EvidenceLevel.METADATA
    )
    relation = classify_relation(evidence_text, active_contract.claim)
    decision, decision_debug = gate_admission_decision(
        work,
        relation,
        intended_relation=active_contract.intended_relation,
        mode=mode,
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
        # G1-G7 identity/existence passed, but that alone is NOT "safe to
        # cite" -- only a matching CiteUse.decision == Decision.ADMIT earns
        # a spot in the public `verified` list (see module docstring).
        # HOLD/REJECT go to `held`/`rejected` instead, and are always still
        # visible in `cite_uses` regardless. This 3-way semantics is
        # IDENTICAL in discovery mode -- discovery is lenient about
        # bibliographic identity only, never about whether the evidence
        # actually supports the claim.
        if decision == Decision.ADMIT:
            verified.append(
                Citation(
                    work=work,
                    context=context,
                    matched_keywords=matched_keywords,
                    thai_relevance=work.primary.thai_relevance,
                )
            )
        else:
            label = f"{work.primary.source_adapter}:{work.primary.source_record_id}"
            entry = {
                "reason": f"admission_{decision.lower()}: {decision_debug.get('reason', decision)}",
                "state": work.state,
                "decision": decision,
                "decision_debug": decision_debug,
                "gate_results": work.gate_results,
                "conflicts": work.conflicts,
                "title": work.primary.title,
                "query": query,
            }
            if decision == Decision.HOLD:
                held[label] = entry
            else:  # Decision.REJECT
                rejected[label] = entry
    else:
        label = f"{work.primary.source_adapter}:{work.primary.source_record_id}"
        rejected[label] = {
            "reason": _rejection_reason(work),
            "state": work.state,
            "gate_results": work.gate_results,
            "conflicts": work.conflicts,
            "title": work.primary.title,
            "query": query,
        }


def discover_citations(
    context: str,
    adapters: list[SourceAdapter],
    context_contract: ContextContract | None = None,
    extra_queries: list[str] | None = None,
) -> dict[str, Any]:
    """Discovery-mode entry point: broad CONTEXT -> real, topically-relevant
    citations -- the `find_cites()` / `thaicite find` path (see
    mcp_server.py, cli.py).

    This is deliberately a SEPARATE function from `resolve_citations()`,
    not a silent mode-flip on it, so a caller can never accidentally search
    with a broad topic string down the strict identity-verification path
    (`verify_cite()`'s job) or vice versa:

      - `resolve_citations()` -- unchanged, identity-verification mode.
        "Is THIS candidate the SAME work as THIS citation string?" Used by
        `verify_cite()` only.
      - `discover_citations()` (this function) -- discovery mode. "What
        real, relevant work exists for this broad topic?" G6's strict
        candidate-vs-query identity match is NOT required here (see
        `evidence/verifier.py::gate_g6_discovery_relevance`); relevance is
        judged by topical overlap between `context` and the candidate
        instead. A real, on-topic paper is never rejected here just
        because its title fails to share 2+ literal tokens with a topic
        phrase that was never meant to BE a citation string.

    Wires in `routing/query_planner.py::plan_queries` (Task 2): rather than
    searching with only the raw `context` string, this builds a small,
    bounded Support x Challenge query family (ARCHITECTURE.md SS91) and
    searches every adapter with every query in that family. Results are
    then FUSED/DEDUPED across the whole family -- a single `resolve_identities()`
    pass over the combined candidate pool -- BEFORE relevance/admission
    logic runs, so a real-world work found via two different query variants
    becomes ONE CanonicalWork/CiteUse, never two competing entries for the
    same work.

    Still returns the SAME 3-way ADMIT/REJECT/HOLD decision shape as
    `resolve_citations()` (plus a `query_family` key showing the
    support/challenge queries actually searched) -- discovery mode is
    lenient about bibliographic IDENTITY matching only; a discovered,
    topically-relevant candidate whose evidence does not clearly support
    the claim still lands on HOLD, never a force-ADMIT (see
    `evidence/verifier.py::gate_admission_decision`, `mode="discovery"`).

    Args:
        context: the broad claim/topic to discover citations for.
        adapters: adapters to search, e.g. `routing.router.route(...).adapters`.
        context_contract: optional frozen scope (see `resolve_citations()`);
            built automatically from `context` when not supplied.
        extra_queries: optional additional raw query strings to search
            alongside the generated Support x Challenge family (e.g. a
            caller-supplied exact phrase); deduped against the family.
    """
    verified: list[Citation] = []
    rejected: dict[str, dict[str, Any]] = {}
    held: dict[str, dict[str, Any]] = {}
    not_found_queries: dict[str, dict[str, Any]] = {}
    cite_uses: list[CiteUse] = []

    active_contract = context_contract or freeze_context(
        claim=context, created_before_search=False
    )

    query_family = plan_queries(context)
    queries = list(
        dict.fromkeys(query_family.get("support", []) + query_family.get("challenge", []))
    )
    if extra_queries:
        queries.extend(q for q in extra_queries if q and q not in queries)
    if not queries:
        # plan_queries() returns empty lists only for an empty/whitespace
        # context -- fall back to the raw context so this never silently
        # searches nothing.
        queries = [context] if context else []

    candidates: list[Candidate] = []
    candidate_origin_query: dict[int, str] = {}

    for query in queries:
        any_hit = False
        found_any_for_query = False
        errors_by_adapter: dict[str, AdapterError] = {}

        for adapter in adapters:
            result = adapter.search(query)
            if isinstance(result, AdapterError):
                errors_by_adapter[adapter.name] = result
                continue
            any_hit = True
            for candidate in adapter.to_candidates(result):
                candidate.thai_relevance = classify_thai_relevance(candidate)
                candidates.append(candidate)
                candidate_origin_query[id(candidate)] = query
                found_any_for_query = True

        if not found_any_for_query:
            non_not_found = {
                name: err
                for name, err in errors_by_adapter.items()
                if err.state != VerificationState.NOT_FOUND
            }
            if non_not_found and not any_hit:
                not_found_queries[f"query::{query}"] = {
                    "state": "adapter_error",
                    "note": (
                        "A real transport/access error occurred for this "
                        "query-family variant -- not a genuine empty result."
                    ),
                    "by_adapter": {
                        name: {"state": err.state, "message": err.message}
                        for name, err in non_not_found.items()
                    },
                }
            else:
                not_found_queries[query] = {
                    "state": VerificationState.NOT_FOUND,
                    "note": (
                        "No adapter returned a record for this query-family "
                        "variant -- this means not found via the adapters "
                        "searched, not that no relevant work exists."
                    ),
                    "by_adapter": {
                        name: {"state": err.state, "message": err.message}
                        for name, err in errors_by_adapter.items()
                    },
                }

    if candidates:
        # Fuse/dedupe across the WHOLE family: one identity-resolution pass
        # over the combined candidate pool (real-identifier / bibliographic
        # merges only -- see resolve/identity.py::can_merge -- so this can
        # never over-merge two genuinely different works just because they
        # share topic vocabulary).
        works = resolve_identities(candidates)

        for work in works:
            origin_queries = sorted(
                {
                    candidate_origin_query[id(c)]
                    for c in work.candidates
                    if id(c) in candidate_origin_query
                }
            )
            query_label = "; ".join(origin_queries) if origin_queries else context
            _evaluate_work(
                work,
                context=context,
                query=query_label,
                active_contract=active_contract,
                mode=VERIFY_MODE_DISCOVERY,
                verified=verified,
                rejected=rejected,
                held=held,
                cite_uses=cite_uses,
            )

    return {
        "verified": verified,
        "rejected": rejected,
        "held": held,
        "not_found_queries": not_found_queries,
        "cite_uses": cite_uses,
        "query_family": query_family,
    }


def _rejection_reason(work) -> str:
    if work.state == VerificationState.CONFLICT:
        fields = ", ".join(c["field"] for c in work.conflicts)
        return f"METADATA_CONFLICT on field(s): {fields}"
    failed_gates = [name for name, passed in work.gate_results.items() if not passed]
    if failed_gates:
        return f"failed gate(s): {', '.join(failed_gates)}"
    return "rejected"
