"""MCP server exposing ThaiCite's 5 tools (2026-09-20 role-change round):

    resolve_source(hint, ...)          # NEW -- reality-anchor (Scout stage)
    fetch_evidence(source_id)          # NEW -- evidence fetch (Reader stage)
    check_claim_evidence(claim, passage, ...)  # NEW -- deterministic Gate
    find_cites(context, max_results=10)        # unchanged public surface
    verify_cite(context, citation)             # unchanged public surface

**BACKGROUND -- why this module looks the way it does (read this before
changing anything here):** ThaiCite has been through 4 rounds of heuristic
fixes to `evidence/statement_type.py`/`evidence/relation.py`, each round
finding the previous one insufficient -- confirming that closed-vocabulary
pattern-matching is the wrong tool for genuinely semantic judgment. The
founder-approved fix is a ROLE CHANGE, not another patch: ThaiCite is called
BY an AI agent over MCP. That calling AI does the SEMANTIC work (proposing
candidate sources, reading passages, judging statement type/relation) as a
"Scout" + "Reader" role, while ThaiCite itself stays a lightweight,
dependency-free deterministic tool doing only:
  1. reality-anchoring (`resolve_source` -- confirm a real record exists via
     a real adapter; the AI cannot self-certify existence),
  2. evidence fetching (`fetch_evidence` -- return the real passage/abstract
     an adapter actually holds, never invented), and
  3. the Gate (`check_claim_evidence` -- ADMIT/REJECT/HOLD, deterministic,
     using the existing statement-type/relation heuristics as a
     CONSISTENCY CHECKER against whatever the AI proposes; disagreement
     between the AI and the checker is INFORMATION, routed to HOLD, never
     silently resolved by trusting one side).

**AI DISCOVERY CONTRACT (applies to every tool below -- this is what the
calling AI needs to know, since a tool's docstring is literally what that
AI reads to decide how to use it):**
  AI (via the calling agent) MAY propose: a title hint, a DOI/PMID hint,
  search terms, a passage-relevance guess, its own read of statement type
  or claim/evidence relation.
  AI MAY NOT claim, and no tool here accepts as truth without independent
  checking: that a source is VERIFIED/CONFIRMED, that a citation is
  safe-to-cite, any bibliographic fact not backed by a real adapter
  record, or an ADMIT decision -- those only ever come from ThaiCite's own
  deterministic resolve/fetch/gate functions, never from what the AI
  asserts about its own proposal.

**Hard constraint honored throughout this module (founder decision,
explicit, this session): no LLM API call is bundled into ThaiCite -- no
`openai`/`anthropic` SDK import, no per-call vendor cost, no API-key
requirement, anywhere in `src/thaicite/`.** The calling AI is already an
LLM by construction of MCP; ThaiCite adds no new vendor dependency.

**`find_cites`/`verify_cite` are BACKWARD-COMPATIBLE WRAPPERS, unchanged in
public signature/return shape.** `verify_cite()` now builds its answer from
the 3 new primitives internally (`resolve_source()` -> `fetch_evidence()`
-> `check_claim_evidence()`, called with no `ai_statement_type`/
`ai_relation` -- pure-deterministic mode, byte-identical decision logic to
the pre-existing `gate_admission_decision()` path since both share
`evidence/verifier.py::_admission_from_resolved_signals()`). `find_cites()`
stays on `core.engine.discover_citations()` deliberately: discovery answers
"what candidate knowledge is reachable for this topic", a question with no
claim to run `check_claim_evidence()` against at all (a topic has no
support/challenge direction -- see `core/models.py::DiscoveredCandidate`'s
own docstring for why this is a structural, not incidental, distinction).
Wiring `find_cites()` through per-candidate `resolve_source()` calls would
also throw away `discover_citations()`'s Support x Challenge query-family
fusion/dedup (`routing/query_planner.py`), which has no per-candidate
primitive equivalent yet -- so `find_cites()` keeps calling
`discover_citations()` directly, exactly as before this round.

**Status of this module:** the `mcp` SDK (`mcp.server.fastmcp.FastMCP`) IS
importable and used for real in this environment -- this is a working
stdio MCP server, not an interface-only stub, wherever that import
succeeds. If `mcp` is not installed in some other environment, this module
still defines every tool-handler function below as a plain, correctly-shaped
Python function (with the exact tool-handler signature/docstring a real MCP
framework would register) and degrades to that interface-only form rather
than failing to import at all -- `HAS_MCP` at module load time tells a
caller which mode it got.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from thaicite.adapters.base import SourceAdapter
from thaicite.adapters.crossref import CrossrefAdapter
from thaicite.adapters.openalex import OpenAlexAdapter
from thaicite.adapters.pubmed import PubMedAdapter
from thaicite.adapters.thaijo import ThaiJOAdapter
from thaicite.core import coverage as cov
from thaicite.core.engine import discover_citations
from thaicite.core.evidence_fetch import STATUS_OK as _FETCH_STATUS_OK
from thaicite.core.evidence_fetch import fetch_evidence as _fetch_evidence
from thaicite.core.models import Decision, DiscoveredCandidate
from thaicite.core.source_resolution import STATUS_CONFIRMED as _SOURCE_CONFIRMED
from thaicite.core.source_resolution import resolve_source as _resolve_source
from thaicite.evidence.verifier import check_claim_evidence as _check_claim_evidence
from thaicite.routing.router import route

try:
    from mcp.server.fastmcp import FastMCP

    HAS_MCP = True
except ImportError:  # pragma: no cover -- exercised only when mcp is absent
    FastMCP = None  # type: ignore[assignment, misc]
    HAS_MCP = False


def _default_adapters() -> list[SourceAdapter]:
    """Same 4 core v1 adapters as `cli.py`. No API keys/contact emails are
    hardcoded here -- each adapter reads its own optional environment
    variable directly (see `adapters/crossref.py`'s `THAICITE_CONTACT_EMAIL`
    handling).
    """
    return [OpenAlexAdapter(), CrossrefAdapter(), ThaiJOAdapter(), PubMedAdapter()]


def _discovered_candidate_to_dict(candidate: DiscoveredCandidate) -> dict[str, Any]:
    """Same field shape as the old `_citation_to_dict()` plus
    `evidence_level`, and deliberately WITHOUT any `verified`/`decision`/
    boolean-admissibility field -- `DiscoveredCandidate` has no such field
    to read one from (see core/models.py), and this function must not
    invent one either. A caller that needs an admissibility verdict for a
    specific claim calls `verify_cite()`, not `find_cites()`.
    """
    primary = candidate.work.primary
    return {
        "title": primary.title,
        "authors": list(primary.authors),
        "year": primary.year,
        "doi": primary.doi,
        "pmid": primary.pmid,
        "source_adapter": primary.source_adapter,
        "source_record_id": primary.source_record_id,
        "url": primary.url,
        "thai_relevance": list(candidate.thai_relevance),
        "matched_keywords": list(candidate.matched_keywords),
        "evidence_level": candidate.evidence_level,
    }


def find_cites(context: str, max_results: int = 10) -> dict[str, Any]:
    """Find real, topically-relevant candidate works for `context`
    (ARCHITECTURE.md SS70).

    This is the tool-handler function registered as the MCP tool
    `find_cites`. Signature and return shape are UNCHANGED by the
    2026-09-20 role-change round (see this module's own docstring for why
    -- discovery has no per-candidate claim to run `check_claim_evidence()`
    against, and `discover_citations()`'s query-family fusion has no
    per-candidate primitive equivalent yet).

    AI DISCOVERY CONTRACT: `context` is the AI's own proposed broad
    topic/claim to discover candidate works for -- ThaiCite never trusts
    that a returned candidate is "the right one" on the AI's say-so; every
    candidate in `candidates` below is independently identity/existence
    confirmed via the same G1-G7 gates every other entry point in this
    project uses (`evidence/verifier.py`). This tool never itself reaches
    an ADMIT/REJECT/HOLD verdict -- call `verify_cite()` (or the new
    `resolve_source`/`fetch_evidence`/`check_claim_evidence` primitives)
    for that, against a specific claim.

    Args:
        context: the broad topic/claim to discover candidate works for.
        max_results: maximum number of candidates to return (default 10).

    Returns:
        {
          "domain": str,                 # routing/router.py's classified domain
          "candidates": [ {..candidate..} ],  # up to max_results real,
                                          # topically-relevant, identity-
                                          # confirmed candidates. No field
                                          # here implies admissibility --
                                          # discovery mode never computes
                                          # one (see core.engine
                                          # .discover_citations()). Call
                                          # verify_cite() to check whether a
                                          # specific candidate is
                                          # admissible for a specific claim.
          "not_found_queries": {...},    # honest "no record found" detail
          "rejected_count": int,         # how many candidates failed a
                                          # G1-G7/relevance gate (existence
                                          # or identity issue only, never an
                                          # admissibility rejection)
          "query_family": {...},         # the support/challenge query
                                          # family actually searched
                                          # (routing/query_planner.py)
          "coverage": [ {...} ],         # Coverage Readout (core/coverage.py):
                                          # one row per adapter (+ ThaiJO's
                                          # per-endpoint rows) tagged
                                          # PLANNED/SEARCHED_OK/STALE/
                                          # UNAVAILABLE/NOT_ATTEMPTED/
                                          # NOT_CONNECTED. ALWAYS present,
                                          # but READ THIS FIELD whenever
                                          # "candidates" is empty -- "no
                                          # candidates" must never be read
                                          # as a universal negative without
                                          # checking which sources this
                                          # `coverage` says were actually,
                                          # freshly (SEARCHED_OK) searched.
          "coverage_all_negative": bool, # True only when NO coverage row
                                          # ever reached a confirmed fresh
                                          # SEARCHED_OK.
          "coverage_has_stale": bool,    # True when at least one coverage
                                          # row is STALE.
        }

    This is DISCOVERY mode (`core.engine.discover_citations()`): it does
    NOT require a candidate's title to bibliographically match the broad
    `context` string the way `verify_cite()`'s identity path does. Relevance
    is judged by topical overlap instead
    (`evidence/verifier.py::gate_g6_discovery_relevance`), and results are
    searched across a small Support x Challenge query family
    (`routing/query_planner.py::plan_queries`), fused/deduped before
    relevance logic runs. `discover_citations()` never computes an
    ADMIT/REJECT/HOLD decision at all.

    Every candidate in the returned list came from `core.engine
    .discover_citations()` -- this function never invents a candidate
    itself (AI NEVER BECOMES THE SOURCE, per core/models.py's module
    docstring).
    """
    adapters = _default_adapters()
    route_decision = route(context=context, query=context, available_adapters=adapters)
    result = discover_citations(context=context, adapters=route_decision.adapters)
    route_decision.update_track_status(result)
    route_decision.update_coverage(result)
    candidates = result["candidates"][:max_results]
    return {
        "domain": route_decision.domain,
        "candidates": [_discovered_candidate_to_dict(c) for c in candidates],
        "not_found_queries": result["not_found_queries"],
        "rejected_count": len(result["rejected"]),
        "query_family": result["query_family"],
        "coverage": [e.to_dict() for e in route_decision.coverage],
        "coverage_all_negative": cov.coverage_is_all_negative(route_decision.coverage),
        "coverage_has_stale": cov.coverage_has_stale(route_decision.coverage),
    }


# ---------------------------------------------------------------------------
# resolve_source / fetch_evidence / check_claim_evidence -- the 3 new
# primitive tools (2026-09-20 role-change round). Each is a thin pass-through
# to its `core`/`evidence` implementation; this module's job is only MCP
# registration + building the default adapter list, exactly like
# `find_cites`/`verify_cite` already do. Every field either tool returns
# traces back to a real adapter record or a plain deterministic computation
# -- nothing here fabricates or reformats evidence.
# ---------------------------------------------------------------------------


def resolve_source_tool(hint: dict[str, Any]) -> dict[str, Any]:
    """Reality-anchor a Scout's proposed source `hint` into a real,
    adapter-confirmed identity, or an honest UNRESOLVED (stage 1 of the
    Scout/Reader contract -- see `core.source_resolution.resolve_source()`,
    which this tool wraps unchanged).

    AI DISCOVERY CONTRACT:
      The calling AI (Scout role) MAY propose, via `hint`: a `title`, a
      `doi`/`pmid`, an `author`, `search_terms` -- any subset, as long as
      at least one field is present.
      The calling AI MAY NOT claim, and this tool does NOT accept as truth
      without independent checking: that a source is VERIFIED/CONFIRMED,
      that a citation is safe-to-cite, any bibliographic fact not backed
      by a real adapter record, or an ADMIT decision. `status` is
      `"SOURCE_CONFIRMED"` ONLY when a real adapter search actually
      returned a matching record and it passed the same identity/existence
      gates (G1-G7) every other ThaiCite entry point uses -- never on the
      calling AI's own say-so that a hinted source exists.
      `"UNRESOLVED"` is returned for everything else, INCLUDING a
      plausible-sounding, fabricated title that no adapter can find.

    Args:
        hint: any of `title`, `doi`, `pmid`, `author`, `search_terms`
            (all optional individually, but at least one non-empty value is
            required).

    Returns: see `core.source_resolution.resolve_source()`'s own docstring
        for the exact `SOURCE_CONFIRMED`/`UNRESOLVED` return shapes
        (`source_id`, `identity`, `match_confidence` on success; `reason`,
        `coverage` on UNRESOLVED). On an empty/invalid `hint`, this tool
        catches the underlying `ValueError` and returns
        `{"status": "UNRESOLVED", "reason": "..."}` instead of raising, so
        an MCP/agent caller always gets a plain dict back.

    No LLM SDK is imported or called anywhere in this module or
    `core.source_resolution` -- the calling AI is already an LLM by
    construction of MCP.
    """
    adapters = _default_adapters()
    try:
        return _resolve_source(hint or {}, adapters=adapters)
    except ValueError as exc:
        return {"status": "UNRESOLVED", "reason": str(exc)}


def fetch_evidence_tool(source_id: str) -> dict[str, Any]:
    """Re-resolve `source_id` (minted by `resolve_source`, or an equivalent
    `"<ADAPTER_NAME>:<native_id>"`/`"DOI:<doi>"`/`"PMID:<pmid>"` token) and
    return the REAL evidence text an adapter actually holds for that
    record, or an honest UNAVAILABLE (stage 2 of the Scout/Reader contract
    -- see `core.evidence_fetch.fetch_evidence()`, which this tool wraps
    unchanged).

    AI DISCOVERY CONTRACT:
      The calling AI (Reader role) MAY propose a `source_id` (from its own
      prior `resolve_source()` call, or any token it has reason to believe
      is real) and MAY read/interpret the returned evidence text -- e.g.
      judge its statement type or claim relation (feed those judgments to
      `check_claim_evidence()` as `ai_statement_type`/`ai_relation`).
      The calling AI MAY NOT claim, and this tool does NOT accept as truth
      without independent checking: that the returned text is a "PASSAGE"
      (full text) when only an abstract or bare metadata was actually
      retrieved, that a source is VERIFIED/CONFIRMED/safe-to-cite, or any
      bibliographic/evidentiary fact not backed by a real adapter response
      this call actually received. `evidence_level` is always the HONEST
      ceiling of what was actually fetched -- `"ABSTRACT"` when abstract
      text is present, `"METADATA"` when only title/authors/year are
      available. No `"PASSAGE"`/full-text level exists yet in this
      codebase's adapters, and this tool never claims one.
      This tool NEVER invents, paraphrases, or summarizes evidence text --
      it only ever returns what an adapter's real response actually
      contains, verbatim.

    Args:
        source_id: a `"<ADAPTER_NAME>:<native_id>"` (e.g.
            `"OPENALEX:https://openalex.org/W123"`) or `"DOI:<doi>"`/
            `"PMID:<pmid>"` token.

    Returns: see `core.evidence_fetch.fetch_evidence()`'s own docstring for
        the exact `OK`/`UNAVAILABLE` return shapes (`evidence_text`,
        `evidence_level`, `locator`, `metadata`, `fetched_at`,
        `source_retrieved_at` on success; `reason` on UNAVAILABLE).

    No LLM SDK is imported or called anywhere in this module or
    `core.evidence_fetch` -- the calling AI is already an LLM by
    construction of MCP.
    """
    adapters = _default_adapters()
    return _fetch_evidence(source_id, adapters=adapters)


def check_claim_evidence_tool(
    claim: str,
    passage: str,
    ai_statement_type: str | None = None,
    ai_relation: str | None = None,
    intended_relation: str = "SUPPORTS",
) -> dict[str, Any]:
    """The deterministic ADMIT/REJECT/HOLD Gate (stage 3 of the Scout/Reader
    contract) -- wraps `evidence.verifier.check_claim_evidence()` unchanged.

    Read `passage` and answer: what is this passage actually asserting, and
    does it support, challenge, qualify, or merely mention the claim? Do
    not try to construct an argument for why this evidence should support
    the claim -- report what you observe, even if it contradicts what you
    expected or hoped to find. Pass your honest read as `ai_statement_type`/
    `ai_relation`; this tool ALWAYS also runs its own independent
    deterministic checker (`evidence.statement_type.classify_statement_type`
    / `evidence.relation.classify_relation`, unchanged) against the same
    `passage`/`claim`, and treats your proposed judgment as an input to
    check, never as truth to trust outright.

    AI DISCOVERY CONTRACT:
      The calling AI (Reader role) MAY propose: its own read of statement
      type (`ai_statement_type`, one of
      `evidence.statement_type.ALL_STATEMENT_TYPES`) or claim/evidence
      relation (`ai_relation`, one of `SUPPORTS`/`CHALLENGES`/
      `CONTEXT_ONLY`/`UNCLEAR`/`QUALIFIES`) for THIS (claim, passage) pair.
      The calling AI MAY NOT claim, and this tool does NOT accept as truth
      without independent checking: that a source is VERIFIED/CONFIRMED,
      that a citation is safe-to-cite, any bibliographic fact, or an ADMIT
      decision -- `decision` only ever comes from this tool's own
      deterministic mapping. If your proposed `ai_statement_type`/
      `ai_relation` DISAGREES with what the independent checker finds,
      `decision` is ALWAYS `"HOLD"` -- disagreement between you and the
      checker is INFORMATION, never silently resolved by trusting either
      side.
      Passing neither `ai_statement_type` nor `ai_relation` (both `None`,
      the default) runs this tool in PURE-DETERMINISTIC mode -- exactly
      what `verify_cite()` uses internally, and appropriate for a caller
      that is not participating in the AI-Reader role for this call (e.g.
      a bare CLI invocation, never an AI participant).

    Args:
        claim: the claim being checked against `passage`.
        passage: the real, fetched evidence text (title/abstract -- never
            invented by the AI; must come from a real adapter record, e.g.
            via `fetch_evidence()`).
        ai_statement_type: the AI Reader's own proposed statement-type
            judgment, or `None`.
        ai_relation: the AI Reader's own proposed relation judgment, or
            `None`.
        intended_relation: which direction the claim needs the evidence to
            point (default `"SUPPORTS"`).

    An `ai_statement_type`/`ai_relation` that is not a legal value is
    silently treated as not supplied (an AI proposal is untrusted free
    text, never crashed on).

    Returns: see `evidence.verifier.check_claim_evidence()`'s own docstring
        for the exact shape (`decision`, `reasons`, `ai_proposed`,
        `deterministic_checked`, `agreement`).

    No LLM SDK is imported or called anywhere in this module or
    `evidence.verifier` -- the calling AI is already an LLM by construction
    of MCP.
    """
    return _check_claim_evidence(
        claim=claim,
        passage=passage,
        ai_statement_type=ai_statement_type,
        ai_relation=ai_relation,
        intended_relation=intended_relation,
    )


# ---------------------------------------------------------------------------
# verify_cite -- BACKWARD-COMPATIBLE WRAPPER, now built from the 3 new
# primitives internally, pure-deterministic mode (no ai_statement_type/
# ai_relation passed -- see this module's own docstring). Public signature
# and return shape are UNCHANGED from before the 2026-09-20 role-change
# round; only the internal implementation is new.
# ---------------------------------------------------------------------------

_DOI_PREFIX_RE = re.compile(r"^(https?://doi\.org/|doi:)", re.IGNORECASE)
_DOI_LIKE_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_PMID_LIKE_RE = re.compile(r"^\d{1,9}$")


def _hint_from_citation(citation: str) -> dict[str, str]:
    """Turn a caller-supplied `citation` string (a title, a DOI, or a PMID
    -- `verify_cite()`'s existing documented `citation` contract, unchanged)
    into the `hint` dict `core.source_resolution.resolve_source()` expects.

    Deliberately simple, deterministic pattern recognition -- not a
    classifier: a DOI-shaped string (`10.<4-9 digits>/<suffix>`, optionally
    prefixed with a DOI URL/scheme) becomes `{"doi": ...}`; an all-digit
    string of plausible PMID length becomes `{"pmid": ...}`; anything else
    is passed through as `{"title": ...}` -- exactly the free-text
    identity-comparison role `citation` already played against
    `evidence.verifier.gate_g6_identity_match` before this round.
    """
    c = (citation or "").strip()
    stripped = _DOI_PREFIX_RE.sub("", c)
    if _DOI_LIKE_RE.match(stripped):
        return {"doi": stripped}
    if _PMID_LIKE_RE.match(c):
        return {"pmid": c}
    return {"title": c}


def _hollow_verify_cite_response(error: str) -> dict[str, Any]:
    return {
        "verified": False,
        "identity_verified": False,
        "decision": None,
        "citation": None,
        "rejected": {},
        "held": {},
        "not_found": {},
        "coverage": [],
        "coverage_all_negative": False,
        "coverage_has_stale": False,
        "error": error,
    }


def verify_cite(context: str, citation: str) -> dict[str, Any]:
    """Verify whether `citation` (a title, DOI, or free-text reference
    string) is a real, resolvable work that fits `context`
    (ARCHITECTURE.md SS70).

    This is the tool-handler function registered as the MCP tool
    `verify_cite`. **Internal implementation, 2026-09-20 role-change round:**
    this function is now a BACKWARD-COMPATIBLE WRAPPER built from the 3 new
    primitives, called in PURE-DETERMINISTIC mode (no `ai_statement_type`/
    `ai_relation` -- see this module's own docstring):

        resolve_source(hint=<parsed from citation>)
          -> fetch_evidence(source_id)
          -> check_claim_evidence(claim=context, passage=evidence_text)

    `check_claim_evidence()`'s decision mapping shares
    `evidence.verifier._admission_from_resolved_signals()` with the
    pre-existing `gate_admission_decision()` path, so this produces the
    SAME ADMIT/REJECT/HOLD outcome the old `resolve_citations()`-based
    implementation did for a VERIFIED work with the same relation/
    statement_type signals. The public signature and return shape below
    are UNCHANGED.

    **Two distinct roles, `context` is REQUIRED and is NEVER defaulted to
    `citation`:**
      - `citation` is the IDENTITY target -- what `resolve_source()`
        anchors against a real adapter record (via `hint`, parsed from
        `citation`; see `_hint_from_citation()`).
      - `context` is the CLAIM -- exactly what `check_claim_evidence()`
        compares the fetched evidence text against for the ADMIT/REJECT/
        HOLD call. `context` must be the caller's own stated claim, never
        the citation string itself -- `citation` answers "which work",
        `context` answers "which claim", and collapsing the two would let
        a paper's own title/abstract trivially SUPPORTS its own title with
        the caller never having stated what they actually wanted to claim.
      A caller who supplies an empty/blank `context` gets a clear `error`
      key in the response (below) instead of `context` silently falling
      back to `citation`.

    Args:
        context: the claim the citation is meant to support (see above).
            REQUIRED -- must be non-empty and distinct in purpose from
            `citation`.
        citation: the citation string to verify (title, DOI, or free
            text) -- the identity target (see above).

    Returns:
        {
          "verified": bool,          # SAFE-TO-CITE gate: True only when
                                      # `decision == "ADMIT"`.
          "identity_verified": bool, # True iff resolve_source() reached
                                      # SOURCE_CONFIRMED -- a real record was
                                      # found and matches `citation`
                                      # bibliographically/by identifier,
                                      # independent of whether it is
                                      # admissible for `context`'s claim.
          "decision": str | None,   # ADMIT/REJECT/HOLD from
                                     # check_claim_evidence(), if identity
                                     # was confirmed; None otherwise.
          "citation": {...} | None, # the matched work's identity, if
                                     # `verified`.
          "rejected": {...},        # rejection detail (a REJECT decision),
                                     # keyed "<source_adapter>:<record_id>",
                                     # if not admitted.
          "held": {...},            # HOLD detail, same keying, if identity
                                     # was confirmed but not (yet)
                                     # admissible.
          "not_found": {...},       # not-found detail, if resolve_source()
                                     # returned UNRESOLVED.
          "coverage": [ {...} ],    # Coverage Readout (core/coverage.py) --
                                     # same shape/semantics as before.
          "coverage_all_negative": bool,
          "coverage_has_stale": bool,
          "error": str,              # present ONLY when `context` was
                                      # missing/blank.
        }

    Raises:
        Nothing -- a missing/blank `context`, or a `citation` too thin for
        `resolve_source()` to anchor against anything, is caught here and
        turned into the `error` key above.
    """
    if not context or not str(context).strip():
        return _hollow_verify_cite_response(
            "verify_cite() requires an explicit, non-empty `context` "
            "(the claim this citation is meant to support) -- it is "
            "never derived from `citation` itself. Pass the actual "
            "claim you want to check this citation as evidence for; "
            "if you only have a broad topic with no specific claim, "
            "use find_cites()/discover_citations() instead."
        )

    adapters = _default_adapters()
    route_decision = route(context=context, query=citation, available_adapters=adapters)

    hint = _hint_from_citation(citation)
    try:
        source_result = _resolve_source(hint, adapters=route_decision.adapters)
    except ValueError as exc:
        return _hollow_verify_cite_response(
            "VERIFY requires an explicit claim distinct from the "
            f"citation identity target: {exc}"
        )

    if source_result["status"] != _SOURCE_CONFIRMED:
        not_found_result = {
            "verified": [],
            "rejected": {},
            "not_found_queries": {citation: {"state": "NOT_FOUND", "note": source_result["reason"]}},
        }
        route_decision.update_track_status(not_found_result)
        route_decision.update_coverage(not_found_result)
        return {
            "verified": False,
            "identity_verified": False,
            "decision": None,
            "citation": None,
            "rejected": {},
            "held": {},
            "not_found": {citation: {"state": "NOT_FOUND", "note": source_result["reason"]}},
            "coverage": [e.to_dict() for e in route_decision.coverage],
            "coverage_all_negative": cov.coverage_is_all_negative(route_decision.coverage),
            "coverage_has_stale": cov.coverage_has_stale(route_decision.coverage),
        }

    identity = source_result["identity"]
    evidence_result = _fetch_evidence(source_result["source_id"], adapters=route_decision.adapters)
    evidence_text = (
        evidence_result.get("evidence_text", "")
        if evidence_result.get("status") == _FETCH_STATUS_OK
        else ""
    )

    # Pure-deterministic mode: no ai_statement_type/ai_relation -- exactly
    # matches gate_admission_decision()'s pre-existing behavior (see
    # check_claim_evidence()'s own docstring, "agreement": None case).
    check_result = _check_claim_evidence(claim=context, passage=evidence_text)
    decision = check_result["decision"]

    fake_candidate = SimpleNamespace(source_adapter=identity["source_adapter"])
    fake_work = SimpleNamespace(candidates=[fake_candidate])
    confirmed_result = {
        "verified": [SimpleNamespace(work=fake_work)],
        "rejected": {},
        "not_found_queries": {},
    }
    route_decision.update_track_status(confirmed_result)
    route_decision.update_coverage(confirmed_result)
    coverage_dicts = [e.to_dict() for e in route_decision.coverage]
    coverage_all_negative = cov.coverage_is_all_negative(route_decision.coverage)
    coverage_has_stale = cov.coverage_has_stale(route_decision.coverage)

    citation_dict = {
        "title": identity["title"],
        "authors": list(identity["authors"]),
        "year": identity["year"],
        "doi": identity["doi"],
        "pmid": identity["pmid"],
        "source_adapter": identity["source_adapter"],
        "source_record_id": identity["source_record_id"],
        "url": identity.get("url"),
        "thai_relevance": list(identity["thai_relevance"]),
        "matched_keywords": [],
    }

    if decision == Decision.ADMIT:
        return {
            "verified": True,
            "identity_verified": True,
            "decision": decision,
            "citation": citation_dict,
            "rejected": {},
            "held": {},
            "not_found": {},
            "coverage": coverage_dicts,
            "coverage_all_negative": coverage_all_negative,
            "coverage_has_stale": coverage_has_stale,
        }

    label = f"{identity['source_adapter']}:{identity['source_record_id']}"
    entry = {
        "reason": f"admission_{decision.lower()}: " + "; ".join(check_result["reasons"]),
        "decision": decision,
        "decision_debug": check_result,
        "title": identity["title"],
        "query": citation,
    }
    held = {label: entry} if decision == Decision.HOLD else {}
    rejected = {label: entry} if decision == Decision.REJECT else {}

    return {
        "verified": False,
        "identity_verified": True,
        "decision": decision,
        "citation": None,
        "rejected": rejected,
        "held": held,
        "not_found": {},
        "coverage": coverage_dicts,
        "coverage_all_negative": coverage_all_negative,
        "coverage_has_stale": coverage_has_stale,
    }


def build_server() -> "FastMCP":
    """Construct the real stdio MCP server, registering all 5 tools: the 3
    new Scout/Reader/Gate primitives plus the 2 backward-compatible
    wrappers. Only callable when `HAS_MCP` is True.
    """
    if not HAS_MCP:
        raise RuntimeError(
            "mcp package is not installed in this environment -- every "
            "tool-handler function in this module is still usable directly "
            "as a plain function, but no stdio MCP server can be "
            "constructed. Install the `mcp` package (`pip install mcp`) to "
            "enable this."
        )

    server = FastMCP("thaicite")

    @server.tool(name="resolve_source")
    def _resolve_source_tool(hint: dict[str, Any]) -> dict[str, Any]:
        """Reality-anchor a proposed source hint. See resolve_source_tool()."""
        return resolve_source_tool(hint=hint)

    @server.tool(name="fetch_evidence")
    def _fetch_evidence_tool(source_id: str) -> dict[str, Any]:
        """Fetch the real evidence text for a resolved source. See fetch_evidence_tool()."""
        return fetch_evidence_tool(source_id=source_id)

    @server.tool(name="check_claim_evidence")
    def _check_claim_evidence_tool(
        claim: str,
        passage: str,
        ai_statement_type: str | None = None,
        ai_relation: str | None = None,
        intended_relation: str = "SUPPORTS",
    ) -> dict[str, Any]:
        """Deterministic ADMIT/REJECT/HOLD gate over a (claim, passage) pair. See check_claim_evidence_tool()."""
        return check_claim_evidence_tool(
            claim=claim,
            passage=passage,
            ai_statement_type=ai_statement_type,
            ai_relation=ai_relation,
            intended_relation=intended_relation,
        )

    @server.tool(name="find_cites")
    def _find_cites_tool(context: str, max_results: int = 10) -> dict[str, Any]:
        """Find verified citations for a claim/context. See find_cites()."""
        return find_cites(context=context, max_results=max_results)

    @server.tool(name="verify_cite")
    def _verify_cite_tool(context: str, citation: str) -> dict[str, Any]:
        """Verify one citation against a claim/context. See verify_cite()."""
        return verify_cite(context=context, citation=citation)

    return server


def main() -> None:
    """Run the stdio MCP server (only when `mcp` is installed)."""
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
