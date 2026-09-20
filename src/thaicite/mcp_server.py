"""Minimal MCP server exposing exactly two tools (ARCHITECTURE.md SS70/SS45):

    find_cites(context, max_results=10)
    verify_cite(context, citation)

ARCHITECTURE.md SS70/SS74 narrows the public interface to exactly these two
calls (`find_cites(context)`, `verify_cite(context, citation)`), and SS45/
SS60 says this ships as a stdio MCP server started per-session, not a
resident daemon. This module is that MCP surface -- a thin wrapper: every
field either tool returns traces back to a real `Citation`/`CiteUse` object
produced by `core.engine.resolve_citations()`; nothing here fabricates or
reformats evidence.

**Status of this module (see task summary for the honest statement):** the
`mcp` SDK (`mcp.server.fastmcp.FastMCP`) IS importable and used for real in
this environment -- this is a working stdio MCP server, not an
interface-only stub, wherever that import succeeds. If `mcp` is not
installed in some other environment, this module still defines
`find_cites`/`verify_cite` as plain, correctly-shaped Python functions (with
the exact tool-handler signature/docstrings a real MCP framework would
register) and degrades to that interface-only form rather than failing to
import at all -- `HAS_MCP` at module load time tells a caller which mode it
got.
"""

from __future__ import annotations

from typing import Any

from thaicite.adapters.base import SourceAdapter
from thaicite.adapters.crossref import CrossrefAdapter
from thaicite.adapters.openalex import OpenAlexAdapter
from thaicite.adapters.pubmed import PubMedAdapter
from thaicite.adapters.thaijo import ThaiJOAdapter
from thaicite.core.engine import discover_citations, resolve_citations
from thaicite.core.models import Citation
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


def _citation_to_dict(citation: Citation) -> dict[str, Any]:
    primary = citation.work.primary
    return {
        "title": primary.title,
        "authors": list(primary.authors),
        "year": primary.year,
        "doi": primary.doi,
        "pmid": primary.pmid,
        "source_adapter": primary.source_adapter,
        "source_record_id": primary.source_record_id,
        "url": primary.url,
        "thai_relevance": list(citation.thai_relevance),
        "matched_keywords": list(citation.matched_keywords),
    }


def find_cites(context: str, max_results: int = 10) -> dict[str, Any]:
    """Find verified citations for `context` (ARCHITECTURE.md SS70).

    This is the tool-handler function registered as the MCP tool
    `find_cites`. Signature and return shape are exactly what an MCP
    framework would call: plain JSON-serializable in, JSON-serializable
    out, no framework object required to invoke it directly (so it is also
    directly unit-testable without a running MCP session).

    Args:
        context: the claim/context to find citations for.
        max_results: maximum number of citations to return (default 10).

    Returns:
        {
          "domain": str,                 # routing/router.py's classified domain
          "citations": [ {..citation..} ],  # up to max_results ADMIT citations
          "not_found_queries": {...},    # honest "no record found" detail
          "rejected_count": int,         # how many candidates failed a gate
                                          # or an admission REJECT
          "held_count": int,             # how many identity-confirmed
                                          # candidates landed on HOLD (not
                                          # yet admissible, never force-ADMITted)
          "query_family": {...},         # the support/challenge query
                                          # family actually searched
                                          # (routing/query_planner.py)
        }

    This is DISCOVERY mode (`core.engine.discover_citations()`): it does
    NOT require a candidate's title to bibliographically match the broad
    `context` string the way `verify_cite()`'s identity path does -- a
    real, on-topic paper is not rejected just because it does not share
    2+ literal tokens with a topic phrase that was never meant to BE a
    citation string. Relevance is judged by topical overlap instead (see
    `evidence/verifier.py::gate_g6_discovery_relevance`), and results are
    searched across a small Support x Challenge query family
    (`routing/query_planner.py::plan_queries`), fused/deduped before
    relevance/admission logic runs. The 3-way ADMIT/REJECT/HOLD semantics
    are unchanged: `citations` only ever contains ADMIT decisions, never a
    force-ADMIT just because discovery mode is lenient about identity.

    Every citation in the returned list came from `core.engine
    .discover_citations()` -- this function never invents a citation
    itself (AI NEVER BECOMES THE SOURCE, per core/models.py's module
    docstring).
    """
    adapters = _default_adapters()
    route_decision = route(context=context, query=context, available_adapters=adapters)
    result = discover_citations(context=context, adapters=route_decision.adapters)
    verified = result["verified"][:max_results]
    return {
        "domain": route_decision.domain,
        "citations": [_citation_to_dict(c) for c in verified],
        "not_found_queries": result["not_found_queries"],
        "rejected_count": len(result["rejected"]),
        "held_count": len(result["held"]),
        "query_family": result["query_family"],
    }


def verify_cite(context: str, citation: str) -> dict[str, Any]:
    """Verify whether `citation` (a title, DOI, or free-text reference
    string) is a real, resolvable work that fits `context`
    (ARCHITECTURE.md SS70).

    This is the tool-handler function registered as the MCP tool
    `verify_cite`. It reuses the exact same `resolve_citations()` pipeline
    as `find_cites` -- `citation` is treated as the query string, so the
    same admission gates (identity/content/scope) apply; there is no
    separate, weaker verification path.

    Args:
        context: the claim/context the citation is meant to support.
        citation: the citation string to verify (title, DOI, or free text).

    Returns:
        {
          "verified": bool,          # SAFE-TO-CITE gate: True only when a
                                      # matching CiteUse.decision == "ADMIT"
                                      # (core/engine.py's `verified` list is
                                      # already filtered on that; this field
                                      # can never be True while `decision` is
                                      # "HOLD" or "REJECT" -- see the
                                      # 2026-09-20 fix note in
                                      # core/engine.py's module docstring).
          "identity_verified": bool, # G1-G7 identity/existence gate only
                                      # (`work.state == VerificationState
                                      # .VERIFIED`) -- a real record was
                                      # found and matches the query
                                      # bibliographically, independent of
                                      # whether it is admissible for the
                                      # claim. This is the field the OLD,
                                      # buggy "verified" used to mean; it
                                      # is NEVER on its own "safe to cite".
          "decision": str | None,   # ADMIT/REJECT/HOLD from the matching
                                     # CiteUse, if any citation-use was
                                     # evaluated for this pair
          "citation": {...} | None, # the matched Citation, if `verified`
          "rejected": {...},        # rejection detail (gate failure OR a
                                     # REJECT admission decision), if not
                                     # admitted
          "held": {...},            # HOLD admission detail, if identity was
                                     # confirmed but not (yet) admissible
          "not_found": {...},       # not-found detail, if nothing matched
        }
    """
    adapters = _default_adapters()
    route_decision = route(context=context, query=citation, available_adapters=adapters)
    result = resolve_citations(
        context=context, queries=[citation], adapters=route_decision.adapters
    )

    identity_verified = any(
        cite_use.work.state == "VERIFIED" for cite_use in result["cite_uses"]
    )

    if result["verified"]:
        matched = result["verified"][0]
        decision = None
        for cite_use in result["cite_uses"]:
            if cite_use.work is matched.work:
                decision = cite_use.decision
                break
        return {
            "verified": True,
            "identity_verified": identity_verified,
            "decision": decision,
            "citation": _citation_to_dict(matched),
            "rejected": {},
            "held": {},
            "not_found": {},
        }

    return {
        "verified": False,
        "identity_verified": identity_verified,
        "decision": (
            result["cite_uses"][0].decision if result["cite_uses"] else None
        ),
        "citation": None,
        "rejected": result["rejected"],
        "held": result["held"],
        "not_found": result["not_found_queries"],
    }


def build_server() -> "FastMCP":
    """Construct the real stdio MCP server, registering exactly the two
    tools SS70/SS45 name. Only callable when `HAS_MCP` is True.
    """
    if not HAS_MCP:
        raise RuntimeError(
            "mcp package is not installed in this environment -- "
            "find_cites()/verify_cite() are still usable directly as plain "
            "functions, but no stdio MCP server can be constructed. "
            "Install the `mcp` package (`pip install mcp`) to enable this."
        )

    server = FastMCP("thaicite")

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
