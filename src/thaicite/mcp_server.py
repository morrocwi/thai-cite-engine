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
from thaicite.core.engine import resolve_citations
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
          "citations": [ {..citation..} ],  # up to max_results VERIFIED citations
          "not_found_queries": {...},    # honest "no record found" detail
          "rejected_count": int,         # how many candidates failed a gate
        }

    Every citation in the returned list came from `core.engine
    .resolve_citations()` -- this function never invents a citation itself
    (AI NEVER BECOMES THE SOURCE, per core/models.py's module docstring).
    """
    adapters = _default_adapters()
    route_decision = route(context=context, query=context, available_adapters=adapters)
    result = resolve_citations(
        context=context, queries=[context], adapters=route_decision.adapters
    )
    verified = result["verified"][:max_results]
    return {
        "domain": route_decision.domain,
        "citations": [_citation_to_dict(c) for c in verified],
        "not_found_queries": result["not_found_queries"],
        "rejected_count": len(result["rejected"]),
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
          "verified": bool,
          "decision": str | None,   # ADMIT/REJECT/HOLD from the matching
                                     # CiteUse, if any citation-use was
                                     # evaluated for this pair
          "citation": {...} | None, # the matched Citation, if VERIFIED
          "rejected": {...},        # rejection detail, if not verified
          "not_found": {...},       # not-found detail, if nothing matched
        }
    """
    adapters = _default_adapters()
    route_decision = route(context=context, query=citation, available_adapters=adapters)
    result = resolve_citations(
        context=context, queries=[citation], adapters=route_decision.adapters
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
            "decision": decision,
            "citation": _citation_to_dict(matched),
            "rejected": {},
            "not_found": {},
        }

    return {
        "verified": False,
        "decision": (
            result["cite_uses"][0].decision if result["cite_uses"] else None
        ),
        "citation": None,
        "rejected": result["rejected"],
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
