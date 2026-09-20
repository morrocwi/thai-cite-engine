"""Reality-anchor primitive: `resolve_source()` turns an AI-proposed *hint*
about a source into either a real, adapter-confirmed identity or an honest
UNRESOLVED, and nothing in between.

**Role context (2026-09-20, MCP role-change round):** ThaiCite is called BY
an AI agent over MCP. That calling AI plays "Scout" (proposing candidate
sources -- a title hint, a DOI/PMID hint, search terms) and "Reader"
(reading passages, judging statement type/relation -- see
`evidence/statement_type.py` and `evidence/relation.py`, kept as
deterministic CONSISTENCY CHECKERS against whatever the AI proposes, not as
the source of truth). ThaiCite itself stays a lightweight, dependency-free
deterministic tool. This module is stage (1) of that contract:
**reality-anchoring** -- confirm a real record exists via a real adapter, on
the AI's behalf, because the AI cannot self-certify existence. Stage (2)
(evidence fetching -- returning the real passage/abstract for a
`source_id` this function minted) and stage (3) (the ADMIT/REJECT/HOLD Gate)
are separate, sibling concerns; `evidence_locator`/`fetch_evidence` is not
implemented in this module -- a `source_id` returned here is the stable
token a sibling `fetch_evidence(source_id)` (or the existing
`verify_cite()`/`resolve_citations()` pipeline) is expected to consume.

**AI DISCOVERY CONTRACT (read this before calling `resolve_source()`):**
  - The calling AI MAY propose, via `hint`: a `title`, a `doi`/`pmid`, an
    `author`, `search_terms` -- any subset, as long as at least one field is
    present.
  - The calling AI MAY NOT claim, and this function does NOT accept as
    truth without independent checking: that a source is VERIFIED/CONFIRMED,
    that a citation is safe-to-cite, any bibliographic fact not backed by a
    real adapter record, or an ADMIT decision. `SOURCE_CONFIRMED` is
    returned ONLY when a real `SourceAdapter.search()` call actually
    returned a matching `Candidate` (which itself cannot be constructed
    without a real `source_adapter`/`source_record_id` -- see
    `core/models.py`'s module docstring, "AI NEVER BECOMES THE SOURCE").
    `UNRESOLVED` is returned for everything else, INCLUDING a
    plausible-sounding, fabricated title that no adapter can find -- this
    function never invents a `source_id` for a hint nothing real backs.

No LLM SDK is imported or called anywhere in this module (explicit founder
constraint, this session): the calling AI is already an LLM by construction
of MCP, so ThaiCite adds no new vendor dependency, no API key requirement,
and no bundled per-call cost.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from thaicite.adapters.base import AdapterError, SourceAdapter
from thaicite.core.models import Candidate, VerificationState
from thaicite.evidence.verifier import (
    VERIFY_MODE_IDENTITY,
    verify,
)
from thaicite.normalize.thai_relevance import classify_thai_relevance
from thaicite.resolve.conflicts import apply_conflict_state
from thaicite.resolve.identity import resolve_identities
from thaicite.routing.router import RouteDecision, route

# Fields a caller may supply in `hint`. At least one must be present.
_HINT_FIELDS = ("title", "doi", "pmid", "author", "search_terms")

STATUS_CONFIRMED = "SOURCE_CONFIRMED"
STATUS_UNRESOLVED = "UNRESOLVED"

MATCH_EXACT_IDENTIFIER = "exact_identifier"
MATCH_BIBLIOGRAPHIC_FUZZY = "bibliographic_fuzzy_match"


def _build_queries(hint: dict[str, Any]) -> tuple[str, str]:
    """Build `(search_query, identity_query)` from `hint`.

    `search_query` is what gets sent to `SourceAdapter.search()`;
    `identity_query` is what `evidence.verifier.gate_g6_identity_match()`
    compares the returned candidate's own identifiers/title/authors
    against, to decide whether it is really the hinted work.

    Identifier precedence (per the task's explicit behavior spec): a
    `doi` or `pmid` hint, when present, drives BOTH queries directly --
    that lets `gate_g6_identity_match`'s PRIORITY-1 exact-identifier
    shortcut (`evidence/verifier.py::_exact_identifier_shortcut`) fire,
    which is strictly stronger evidence of identity than any fuzzy
    title/author match and must never be diluted by mixing in other hint
    fields. Otherwise, `title`/`author`/`search_terms` drive the search
    (all supplied fields are combined so the adapter sees the caller's
    full signal) while `identity_query` stays the most identity-specific
    single field available (`title` first, since G6's fuzzy matcher
    compares against the candidate's own title) so a long, noisy
    `search_terms` string never dilutes the token-overlap ratio.
    """
    doi = (hint.get("doi") or "").strip() if hint.get("doi") else ""
    pmid = (hint.get("pmid") or "").strip() if hint.get("pmid") else ""
    title = (hint.get("title") or "").strip() if hint.get("title") else ""
    author = (hint.get("author") or "").strip() if hint.get("author") else ""
    search_terms = (hint.get("search_terms") or "").strip() if hint.get("search_terms") else ""

    identifier = doi or pmid
    if identifier:
        return identifier, identifier

    identity_query = title or search_terms or author
    search_query = " ".join(part for part in (title, search_terms, author) if part)
    return search_query, identity_query


def _search_query(
    query: str, adapters: list[SourceAdapter]
) -> tuple[list[Candidate], dict[str, AdapterError]]:
    """Run `query` against every adapter, exactly like
    `core.engine.resolve_citations()`'s inner loop -- every field on every
    returned `Candidate` traces back to a real adapter response (see
    `adapters/base.py`); this function never fabricates one.
    """
    candidates: list[Candidate] = []
    errors_by_adapter: dict[str, AdapterError] = {}
    for adapter in adapters:
        result = adapter.search(query)
        if isinstance(result, AdapterError):
            errors_by_adapter[adapter.name] = result
            continue
        for candidate in adapter.to_candidates(result):
            candidate.thai_relevance = classify_thai_relevance(candidate)
            candidates.append(candidate)
    return candidates, errors_by_adapter


def _coverage_readout(route_decision: RouteDecision) -> dict[str, Any]:
    return {
        "domain": route_decision.domain,
        "entries": [e.to_dict() for e in route_decision.coverage],
        "track_status": dict(route_decision.track_status),
    }


def resolve_source(hint: dict[str, Any], adapters: list[SourceAdapter]) -> dict[str, Any]:
    """Resolve an AI-proposed `hint` into a real, adapter-confirmed source
    identity, or an honest `UNRESOLVED` -- the reality-anchor primitive
    (see module docstring for the full AI Discovery Contract).

    Args:
        hint: any of `title`, `doi`, `pmid`, `author`, `search_terms`
            (all optional individually, but at least one non-empty field
            is required -- a `ValueError` is raised otherwise, since a
            fully-empty hint has nothing to anchor against reality).
        adapters: the real `SourceAdapter` instances to search (e.g.
            `[OpenAlexAdapter(), CrossrefAdapter(), ThaiJOAdapter(),
            PubMedAdapter()]`, or the subset a caller already has
            configured).

    Behavior:
      - `doi`/`pmid` present -> searched as the primary, identifier-driven
        query; a real record whose own `doi`/`pmid`/`pmcid`/
        `source_record_id` exactly matches (via
        `evidence.verifier.gate_g6_identity_match`'s exact-identifier
        shortcut, reused unmodified) resolves with
        `match_confidence="exact_identifier"`.
      - Otherwise, `title`/`author`/`search_terms` drive the search via
        the existing adapter/router infrastructure --
        `routing/router.py::route()`'s Thai-first domain routing is
        reused as-is, never bypassed, so a Thai-signal hint still queries
        ThaiJO first exactly as every other entry point does. A match
        found this way resolves with
        `match_confidence="bibliographic_fuzzy_match"`.
      - **Reality-anchor guarantee**: this function reaches
        `SOURCE_CONFIRMED` ONLY by running the exact same `verify()`
        identity gate (`evidence/verifier.py::verify`,
        `mode=VERIFY_MODE_IDENTITY`) used everywhere else in this project
        against a `CanonicalWork` built from real `Candidate` objects a
        real adapter actually returned. It never accepts the caller's
        (i.e. the AI's) own assertion that a hinted source exists as
        sufficient, and it never invents a `source_id` for a hint that no
        adapter confirms -- a plausible-sounding fabricated title
        resolves to `UNRESOLVED`, honestly, every time (see this
        module's docstring and `tests/test_source_resolution.py`'s
        `test_fabricated_title_resolves_unresolved` for a worked
        example).

    Returns:
        On a confirmed match::

            {
              "status": "SOURCE_CONFIRMED",
              "source_id": "<ADAPTER>:<source_record_id>",  # a stable
                  # token, e.g. "OPENALEX:https://openalex.org/W2741809807"
                  # or "OPENALEX:10.1234/abcd" -- always
                  # "<candidate.source_adapter>:<candidate.source_record_id>",
                  # documented here as the contract a sibling
                  # `fetch_evidence(source_id)` (or the existing
                  # `verify_cite()`/`resolve_citations()` pipeline, which
                  # already keys off the same source_adapter/
                  # source_record_id pair) is expected to parse on
                  # ":" -- split once, adapter name before, the adapter's
                  # own record id after (a DOI-shaped record id may itself
                  # contain ":" or "/", so callers must split on the FIRST
                  # ":" only).
              "identity": {
                "title": str, "authors": list[str], "year": int | None,
                "doi": str | None, "pmid": str | None,
                "source_adapter": str, "source_record_id": str,
                "url": str | None,
                "thai_relevance": list[str],
              },
              "match_confidence": "exact_identifier" | "bibliographic_fuzzy_match",
            }

        When nothing resolves::

            {
              "status": "UNRESOLVED",
              "reason": str,       # human-readable, honest explanation
              "coverage": {
                "domain": str,               # routing/router.py's classified domain
                "entries": [ {...} ],        # core/coverage.py CoverageEntry
                                              # dicts -- PLANNED/SEARCHED_OK/
                                              # STALE/UNAVAILABLE/
                                              # NOT_ATTEMPTED/NOT_CONNECTED,
                                              # refined with real post-search
                                              # evidence via
                                              # RouteDecision.update_coverage()
                                              # -- see core/coverage.py's
                                              # "0 != bottom" / "not found in
                                              # THIS readout" discipline.
                "track_status": dict,        # coarse global/local health,
                                              # RouteDecision.update_track_status()
              },
            }

    Raises:
        ValueError: `hint` carries none of `title`/`doi`/`pmid`/`author`/
            `search_terms` as a non-empty value.
    """
    def _has_value(field: str) -> bool:
        value = hint.get(field)
        return bool(value.strip()) if isinstance(value, str) else bool(value)

    if not any(_has_value(field) for field in _HINT_FIELDS):
        raise ValueError(
            "resolve_source() requires `hint` to carry at least one "
            f"non-empty field among {_HINT_FIELDS} -- an empty hint has "
            "nothing to anchor against a real source record."
        )

    search_query, identity_query = _build_queries(hint)
    identifier_driven = bool(
        (hint.get("doi") or "").strip() if isinstance(hint.get("doi"), str) else hint.get("doi")
    ) or bool(
        (hint.get("pmid") or "").strip() if isinstance(hint.get("pmid"), str) else hint.get("pmid")
    )

    route_decision = route(
        context=identity_query, query=search_query, available_adapters=adapters
    )

    candidates, errors_by_adapter = _search_query(search_query, route_decision.adapters)

    if not candidates:
        non_not_found = {
            name: err
            for name, err in errors_by_adapter.items()
            if err.state != VerificationState.NOT_FOUND
        }
        engine_result: dict[str, Any] = {"verified": [], "candidates": [], "rejected": {}, "not_found_queries": {}}
        by_adapter = {
            name: {"state": err.state, "message": err.message, "coverage": err.coverage}
            for name, err in errors_by_adapter.items()
        }
        if non_not_found:
            engine_result["rejected"] = {f"query::{search_query}": {"reason": "adapter_error", "by_adapter": by_adapter}}
            reason = (
                "A real transport/access error occurred on every adapter "
                "searched -- this is not a confirmed absence, see coverage."
            )
        else:
            engine_result["not_found_queries"] = {
                search_query: {"state": VerificationState.NOT_FOUND, "note": "no record", "by_adapter": by_adapter}
            }
            reason = (
                "No adapter returned a record matching this hint -- this "
                "means not found via the adapters searched, not that the "
                "source does not exist."
            )
        route_decision.update_track_status(engine_result)
        route_decision.update_coverage(engine_result)
        return {
            "status": STATUS_UNRESOLVED,
            "reason": reason,
            "coverage": _coverage_readout(route_decision),
        }

    works = resolve_identities(candidates)

    confirmed_work = None
    confirmed_debug: dict[str, Any] = {}
    verified_works = []
    # Every work resolve_identities() found, verified or not -- used below
    # to give update_coverage()/update_track_status() real evidence that an
    # adapter genuinely searched and returned a (non-matching) record, not
    # just for the confirmed match. Without this, an adapter that returned
    # candidates none of which passed identity would be left at its a-priori
    # PLANNED/OK, silently indistinguishable from "never actually searched".
    non_verified_labels: dict[str, dict[str, Any]] = {}
    for work in works:
        apply_conflict_state(work)
        work, _matched_keywords = verify(
            work, context="", query=identity_query, mode=VERIFY_MODE_IDENTITY
        )
        if work.state == VerificationState.VERIFIED:
            verified_works.append(work)
        else:
            label = f"{work.primary.source_adapter}:{work.primary.source_record_id}"
            non_verified_labels[label] = {
                "reason": "failed_identity_or_existence_gate",
                "gate_results": work.gate_results,
            }

    # Prefer an exact-identifier match over a bibliographic-fuzzy one --
    # strictly stronger evidence of identity (see
    # evidence/verifier.py::gate_g6_identity_match's own priority order).
    for work in verified_works:
        debug = work.g6_identity_debug.get("identity", {})
        if debug.get("reason") == "exact_identifier_match":
            confirmed_work, confirmed_debug = work, debug
            break
    # An identifier-driven hint (doi/pmid) must NEVER fall back to a fuzzy
    # bibliographic match -- confirmed live 2026-09-20: searching an adapter's
    # general-text `search()` with a raw DOI string as the query does not
    # reliably retrieve the record that DOI actually identifies (adapters
    # here expose full-text search, not an identifier-lookup endpoint), and
    # a DOI/PMID's own digit-heavy token shape can coincidentally
    # token-overlap with an UNRELATED candidate's title/metadata (e.g. a
    # different work's own embedded DOI string sharing several numeric
    # tokens) -- clearing gate_g6_identity_match's fuzzy threshold for the
    # WRONG record. Accepting that would be a real reality-anchor failure
    # (a real record, but not the one the identifier names), so for
    # identifier-driven hints only an exact_identifier_match may confirm;
    # a title/author hint (no doi/pmid) still uses the fuzzy fallback below,
    # exactly as before.
    if confirmed_work is None and verified_works and not identifier_driven:
        confirmed_work = verified_works[0]
        confirmed_debug = confirmed_work.g6_identity_debug.get("identity", {})

    engine_result = {
        "verified": [SimpleNamespace(work=w) for w in verified_works],
        "candidates": [],
        "rejected": non_verified_labels,
        "not_found_queries": {},
    }
    route_decision.update_track_status(engine_result)
    route_decision.update_coverage(engine_result)

    if confirmed_work is None:
        reason = (
            "Adapter(s) returned record(s) for this hint, but none "
            "passed the real identity check (G1-G7) against it -- see "
            "coverage; this hint's candidate(s) did not survive "
            "reality-anchoring."
        )
        if identifier_driven and verified_works:
            reason = (
                "Adapter(s) returned record(s) that passed a fuzzy "
                "bibliographic identity check, but none exactly matched "
                "the doi/pmid identifier given -- an identifier hint is "
                "never confirmed by a fuzzy fallback (see this function's "
                "docstring), so this is honestly reported as UNRESOLVED "
                "rather than resolving to a possibly-wrong record."
            )
        return {
            "status": STATUS_UNRESOLVED,
            "reason": reason,
            "coverage": _coverage_readout(route_decision),
        }

    primary = confirmed_work.primary
    match_confidence = (
        MATCH_EXACT_IDENTIFIER
        if confirmed_debug.get("reason") == "exact_identifier_match"
        else MATCH_BIBLIOGRAPHIC_FUZZY
    )
    return {
        "status": STATUS_CONFIRMED,
        "source_id": f"{primary.source_adapter}:{primary.source_record_id}",
        "identity": {
            "title": primary.title,
            "authors": list(primary.authors),
            "year": primary.year,
            "doi": primary.doi,
            "pmid": primary.pmid,
            "source_adapter": primary.source_adapter,
            "source_record_id": primary.source_record_id,
            "url": primary.url,
            "thai_relevance": list(primary.thai_relevance),
        },
        "match_confidence": match_confidence,
    }
