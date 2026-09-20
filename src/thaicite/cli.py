"""Command-line entry point: `thaicite find --context "..." --max N`.

Thin wrapper only -- exactly the pattern ARCHITECTURE.md SS60/SS61 describes
for v1 ("no resident daemon; Python package ... e.g. `thaicite find
'context...'`"). This module owns none of the actual logic: it builds the
default adapter list, calls `routing.router.route()` to pick/order adapters
for the (context, query) pair, then `core.engine.resolve_citations()` to
turn that into verified citations, and prints the result. Every field
printed here traces back to a real `Citation`/`CiteUse` object -- this
module never invents or reformats evidence, only presents it.

Registered as the `thaicite` console-script entry point in pyproject.toml
(`thaicite = "thaicite.cli:main"`).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from thaicite.adapters.base import SourceAdapter
from thaicite.adapters.crossref import CrossrefAdapter
from thaicite.adapters.openalex import OpenAlexAdapter
from thaicite.adapters.pubmed import PubMedAdapter
from thaicite.adapters.thaijo import ThaiJOAdapter
from thaicite.core import coverage as cov
from thaicite.core.engine import discover_citations
from thaicite.core.models import Citation, DiscoveredCandidate
from thaicite.routing.router import route


def _default_adapters() -> list[SourceAdapter]:
    """Build the 4 core v1 adapters (ARCHITECTURE.md SS56).

    No API keys/contact emails are hardcoded anywhere here -- each adapter
    reads its own optional environment variable directly (e.g.
    `THAICITE_CONTACT_EMAIL` for Crossref's polite pool, per that adapter's
    own module docstring); this function never touches those values.
    """
    return [OpenAlexAdapter(), CrossrefAdapter(), ThaiJOAdapter(), PubMedAdapter()]


def _format_citation(citation: Citation, index: int) -> str:
    work = citation.work
    primary = work.primary
    authors = ", ".join(primary.authors) if primary.authors else "(no author listed)"
    year = primary.year if primary.year is not None else "n.d."
    ident_bits = []
    if primary.doi:
        ident_bits.append(f"doi:{primary.doi}")
    if primary.pmid:
        ident_bits.append(f"pmid:{primary.pmid}")
    if not ident_bits:
        ident_bits.append(f"{primary.source_adapter}:{primary.source_record_id}")
    ident = " ".join(ident_bits)
    thai_tag = f" [{'/'.join(citation.thai_relevance)}]" if citation.thai_relevance else ""
    return f"{index}. {authors} ({year}). {primary.title}. {ident}{thai_tag}"


def _format_candidate(candidate: DiscoveredCandidate, index: int) -> str:
    """Same shape as `_format_citation()` for a `DiscoveredCandidate` --
    note there is no admissibility verdict to print here (that type has no
    `decision` field); `verify_cite`/`thaicite`'s identity path is where
    that question is answered, for a specific claim.
    """
    work = candidate.work
    primary = work.primary
    authors = ", ".join(primary.authors) if primary.authors else "(no author listed)"
    year = primary.year if primary.year is not None else "n.d."
    ident_bits = []
    if primary.doi:
        ident_bits.append(f"doi:{primary.doi}")
    if primary.pmid:
        ident_bits.append(f"pmid:{primary.pmid}")
    if not ident_bits:
        ident_bits.append(f"{primary.source_adapter}:{primary.source_record_id}")
    ident = " ".join(ident_bits)
    thai_tag = f" [{'/'.join(candidate.thai_relevance)}]" if candidate.thai_relevance else ""
    return f"{index}. {authors} ({year}). {primary.title}. {ident}{thai_tag}"


def _print_debug_detail(result: dict[str, Any]) -> None:
    rejected = result.get("rejected") or {}
    not_found = result.get("not_found_queries") or {}

    print("\n--- debug: rejected (existence/identity/relevance gate failures) ---")
    if not rejected:
        print("(none)")
    for label, detail in rejected.items():
        print(f"- {label}: {detail.get('reason')} (state={detail.get('state')})")
        if detail.get("title"):
            print(f"    title: {detail['title']}")
        if detail.get("query"):
            print(f"    query: {detail['query']}")

    print("\n--- debug: not_found_queries ---")
    if not not_found:
        print("(none)")
    for query, detail in not_found.items():
        print(f"- {query!r}: {detail.get('note')}")
        for adapter_name, info in (detail.get("by_adapter") or {}).items():
            print(f"    {adapter_name}: {info.get('state')} -- {info.get('message')}")
            if info.get("coverage"):
                print(f"      coverage: {info['coverage']}")


def find_citations(context: str, max_results: int, debug: bool = False) -> int:
    """Run one `find` request end-to-end and print the results.

    Discovery mode (`core.engine.discover_citations()`): searches a small,
    bounded Support x Challenge query family generated from `context`
    (`routing.query_planner.plan_queries`), fuses/dedupes across it, and
    judges relevance by topical overlap rather than requiring the broad
    `context` string to bibliographically match a candidate's title --
    see `discover_citations()`'s docstring.

    Returns the process exit code (0 on success, even when no citations
    were found -- an empty result is a legitimate outcome, not a failure).
    """
    adapters = _default_adapters()
    route_decision = route(context=context, query=context, available_adapters=adapters)

    result = discover_citations(
        context=context,
        adapters=route_decision.adapters,
    )
    route_decision.update_track_status(result)
    route_decision.update_coverage(result)

    candidates: list[DiscoveredCandidate] = result["candidates"][:max_results]

    print(f"domain: {route_decision.domain}")
    print(f"track_status: {route_decision.track_status}")
    if debug:
        print(f"query_family: {result.get('query_family')}")
    print()

    if not candidates:
        # Coverage Readout (founder-approved redesign, 2026-09-20): "not
        # found" must NEVER print alone -- it always prints together with
        # which sources were actually searchable, so an empty result never
        # silently reads as a universal negative. See core/coverage.py.
        print("No candidate works found for this context.")
        print()
        print("Coverage -- ไม่พบงาน (nothing found), ภายใต้แหล่งที่ค้นได้เหล่านี้ (given these searchable sources):")
        for line in cov.format_coverage_lines(route_decision.coverage):
            print(line)
        if cov.coverage_is_all_negative(route_decision.coverage):
            print(
                "  ** WARNING: every listed source was UNAVAILABLE/NOT_ATTEMPTED/"
                "NOT_CONNECTED -- this 'not found' reflects a coverage failure, "
                "not a confirmed absence. **"
            )
    else:
        print(f"Found {len(candidates)} candidate(s):\n")
        for i, candidate in enumerate(candidates, start=1):
            print(_format_candidate(candidate, i))

    if debug:
        _print_debug_detail(result)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="thaicite",
        description="ThaiCite CLI -- find verified citations for a context.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    find_parser = subparsers.add_parser(
        "find", help="Find verified citations for a context string."
    )
    find_parser.add_argument(
        "--context",
        required=True,
        help="The claim/context to find citations for.",
    )
    find_parser.add_argument(
        "--max",
        dest="max_results",
        type=int,
        default=10,
        help="Maximum number of citations to print (default: 10).",
    )
    find_parser.add_argument(
        "--debug",
        action="store_true",
        help="Also print rejected/not_found/HOLD detail.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "find":
        return find_citations(
            context=args.context, max_results=args.max_results, debug=args.debug
        )

    parser.error(f"Unknown command: {args.command!r}")
    return 2  # pragma: no cover -- argparse.error() already exits


if __name__ == "__main__":
    sys.exit(main())
