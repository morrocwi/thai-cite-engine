"""Evidence-fetch primitive: `fetch_evidence(source_id, adapters)` turns a
`source_id` token -- minted by `core.source_resolution.resolve_source()`, or
any equivalently-shaped identifier a caller already has -- into the REAL
evidence text an adapter actually holds for that record, or an honest
`UNAVAILABLE`.

**Role context (2026-09-20, MCP role-change round, stage (2) of the
Scout/Reader contract -- see `core/source_resolution.py`'s module docstring
for stages (1) and (3)):** ThaiCite is called BY an AI agent over MCP. That
calling AI plays "Scout" (`resolve_source()`, stage 1) and "Reader" (reading
the evidence this module returns and judging statement type/relation --
`evidence/statement_type.py`, `evidence/relation.py`). This module is stage
(2): **evidence fetching** -- return the real passage/abstract this adapter
has for a `source_id`, on the AI's behalf, because the AI cannot self-certify
what a source says any more than it can self-certify that the source exists.

**AI DISCOVERY CONTRACT (read this before calling `fetch_evidence()`):**
  - The calling AI MAY propose a `source_id` (from its own prior
    `resolve_source()` call, or any `"<ADAPTER_NAME>:<native_id>"` /
    `"DOI:<doi>"` / `"PMID:<pmid>"` token it has reason to believe is real)
    and MAY read/interpret the returned evidence text -- e.g. judge its
    statement type or claim relation.
  - The calling AI MAY NOT claim, and this function does NOT accept as
    truth without independent checking: that the returned text is a
    "PASSAGE" (full-text) when only an abstract or bare metadata was ever
    actually retrieved, that a source is VERIFIED/CONFIRMED/safe-to-cite, or
    any bibliographic/evidentiary fact not backed by a real adapter response
    this call actually received. `evidence_level` is always the HONEST
    ceiling of what was actually fetched (`"ABSTRACT"` when abstract text is
    present, `"METADATA"` when only title/authors/year are available -- no
    `"PASSAGE"`/full-text level exists yet in this codebase's adapters, and
    this function never claims one).
  - This function NEVER invents, paraphrases, or summarizes evidence text --
    it only ever returns what an adapter's real response actually contains,
    verbatim (the abstract string an adapter's own `to_candidates()`
    reconstructed from that adapter's real API response). If the source
    cannot be re-resolved, the honest answer is `{"status": "UNAVAILABLE",
    "reason": "..."}`, never fabricated text standing in for it.

No LLM SDK is imported or called anywhere in this module (same explicit
founder constraint as `core/source_resolution.py`): the calling AI is
already an LLM by construction of MCP, so ThaiCite adds no new vendor
dependency, no API key requirement, and no bundled per-call cost.
"""

from __future__ import annotations

import time
from typing import Any

from thaicite.adapters.base import AdapterError, SourceAdapter
from thaicite.core.models import Candidate

STATUS_OK = "OK"
STATUS_UNAVAILABLE = "UNAVAILABLE"

EVIDENCE_LEVEL_ABSTRACT = "ABSTRACT"
EVIDENCE_LEVEL_METADATA = "METADATA"

LOCATOR_ABSTRACT = "abstract"
LOCATOR_METADATA_ONLY = "metadata-only"

# Identifier-style prefixes that name an identifier FIELD to re-search by,
# rather than an adapter NAME -- see `_parse_source_id()`. Matched
# case-insensitively against the token before the first ":".
_IDENTIFIER_PREFIXES = frozenset({"DOI", "PMID"})


def _parse_source_id(source_id: str) -> tuple[str, str] | None:
    """Split `source_id` into `(kind, value)` on the FIRST ":" only.

    `kind` is either a real adapter name (as `SourceAdapter.name` spells it,
    e.g. `"OPENALEX"`) matched case-insensitively against `adapters`, or one
    of the identifier-style prefixes in `_IDENTIFIER_PREFIXES` (`"DOI"`,
    `"PMID"`) -- both forms are accepted per this function's documented
    contract (`"<ADAPTER_NAME>:<native_id>"` or `"DOI:<doi>"`/`"PMID:<pmid>"`,
    reconciled with `core.source_resolution.resolve_source()`'s own
    `"<candidate.source_adapter>:<candidate.source_record_id>"` token shape,
    which is exactly the adapter-name form).

    A DOI value may itself legitimately contain ":" (rare, but the DOI
    namespace does not forbid it) or "/", so only the FIRST ":" is treated
    as the separator, never a `split(":")` that could shear a real DOI in
    two.

    Returns `None` when `source_id` has no ":" at all (malformed -- nothing
    to parse a kind/value pair out of).
    """
    if not source_id or ":" not in source_id:
        return None
    kind, _, value = source_id.partition(":")
    kind = kind.strip()
    value = value.strip()
    if not kind or not value:
        return None
    return kind, value


def _find_adapter_by_name(kind: str, adapters: list[SourceAdapter]) -> SourceAdapter | None:
    kind_upper = kind.upper()
    for adapter in adapters:
        if adapter.name.upper() == kind_upper:
            return adapter
    return None


def _search_all(
    query: str, adapters: list[SourceAdapter]
) -> tuple[list[Candidate], dict[str, AdapterError]]:
    """Run `query` against every adapter in `adapters`, exactly like
    `core.source_resolution._search_query()` / `core.engine.resolve_citations()`'s
    inner loop -- every field on every returned `Candidate` traces back to a
    real adapter response; this function never fabricates one.
    """
    candidates: list[Candidate] = []
    errors_by_adapter: dict[str, AdapterError] = {}
    for adapter in adapters:
        result = adapter.search(query)
        if isinstance(result, AdapterError):
            errors_by_adapter[adapter.name] = result
            continue
        candidates.extend(adapter.to_candidates(result))
    return candidates, errors_by_adapter


def _matches_source_id(candidate: Candidate, kind: str, value: str) -> bool:
    """Does `candidate` correspond to the record `(kind, value)` named?

    For an adapter-name `kind`, requires the candidate to actually have come
    back from that same adapter AND its own `source_record_id` to exactly
    match `value` -- re-running `search()` with an id-shaped query string can
    legitimately return OTHER records too (adapters here expose full-text
    search, not an identifier-lookup endpoint -- the same reason
    `core/source_resolution.py` never falls back to a fuzzy match for an
    identifier-driven hint), so this function only ever accepts an EXACT
    `source_record_id` match, never "the first/closest result".

    For a `"DOI"`/`"PMID"` `kind`, matches on the candidate's own `doi`/
    `pmid` field (normalized the same way `evidence/verifier.py
    ::_normalize_identifier` does: stripped, lowercased, common URL/scheme
    prefixes removed) across ANY adapter, since a DOI/PMID is
    adapter-independent by construction.
    """
    if kind.upper() in _IDENTIFIER_PREFIXES:
        field = "doi" if kind.upper() == "DOI" else "pmid"
        candidate_value = getattr(candidate, field, None)
        return _normalize_identifier(candidate_value) == _normalize_identifier(value)
    return (
        candidate.source_adapter.upper() == kind.upper()
        and candidate.source_record_id == value
    )


def _normalize_identifier(value: str | None) -> str:
    """Same normalization as `evidence/verifier.py::_normalize_identifier`
    (duplicated rather than imported, to keep this module's only dependency
    on `evidence/` at zero -- reality-anchoring/evidence-fetching stay
    lightweight primitives independent of the relation/statement-type
    consistency-checker layer)."""
    v = (value or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:", "pmid:", "pmcid:"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    return v.strip()


def fetch_evidence(source_id: str, adapters: list[SourceAdapter]) -> dict[str, Any]:
    """Re-resolve `source_id` against `adapters` and return the REAL
    evidence text (abstract or, failing that, honest metadata-only) an
    adapter actually has for that record.

    Args:
        source_id: a stable token shaped `"<ADAPTER_NAME>:<native_id>"`
            (exactly what `core.source_resolution.resolve_source()` mints
            as `source_id`, e.g. `"OPENALEX:https://openalex.org/W123"`) or
            `"DOI:<doi>"` / `"PMID:<pmid>"` (a caller-supplied
            self-describing identifier token any adapter search can be
            re-run against, when no prior `resolve_source()` call produced
            an adapter-scoped id). Split on the FIRST ":" only.
        adapters: the real `SourceAdapter` instances to re-query (e.g.
            `[OpenAlexAdapter(), CrossrefAdapter(), ThaiJOAdapter(),
            PubMedAdapter()]`, or the subset a caller already has
            configured). For an adapter-name `source_id`, only the matching
            adapter is queried; for a `DOI`/`PMID` `source_id`, every
            adapter in this list is queried (a DOI/PMID is
            adapter-independent).

    Returns:
        On success::

            {
              "status": "OK",
              "source_id": str,          # echoed back, unchanged
              "evidence_text": str,      # the REAL abstract text this
                                          # adapter's response contains for
                                          # this record -- never invented,
                                          # never paraphrased. Empty string
                                          # only paired with
                                          # evidence_level="METADATA" (see
                                          # below), never claimed as an
                                          # abstract that does not exist.
              "evidence_level": "ABSTRACT" | "METADATA",  # honest ceiling of
                                          # what was actually fetched -- see
                                          # module docstring. No "PASSAGE"
                                          # level exists yet in this
                                          # codebase's adapters.
              "locator": "abstract" | "metadata-only",
              "metadata": {               # always present, always real --
                  "title": str, "authors": list[str], "year": int | None,
                  "doi": str | None, "pmid": str | None,
                  "source_adapter": str, "source_record_id": str,
                  "url": str | None,
              },
              "fetched_at": float,        # raw Unix timestamp of THIS fetch
                                          # (time.time() at call time) --
                                          # distinct from the candidate's own
                                          # `retrieved_at` on the underlying
                                          # adapter response, which is also
                                          # surfaced as
                                          # "source_retrieved_at" below.
              "source_retrieved_at": float,  # the underlying Candidate's own
                                          # `retrieved_at` -- when the
                                          # adapter itself says it retrieved
                                          # this record, which may predate
                                          # this specific fetch_evidence()
                                          # call if the adapter caches.
            }

        When the source cannot be re-resolved (malformed/unknown
        `source_id`, the adapter it names is not in `adapters`, every
        adapter search failed/errored, or no returned candidate's own
        `source_record_id`/`doi`/`pmid` exactly matches what `source_id`
        names)::

            {
              "status": "UNAVAILABLE",
              "reason": str,             # honest, specific explanation --
                                          # never a placeholder standing in
                                          # for real evidence text.
            }

    This function NEVER fabricates or reformats evidence text -- every
    field in a successful return traces back to a real
    `SourceAdapter.search()` response re-run just now (see module
    docstring's AI Discovery Contract).
    """
    parsed = _parse_source_id(source_id)
    if parsed is None:
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": (
                f"source_id {source_id!r} is malformed -- expected "
                '"<ADAPTER_NAME>:<native_id>" (e.g. "OPENALEX:https://'
                'openalex.org/W123") or "DOI:<doi>"/"PMID:<pmid>", split on '
                "the first \":\"."
            ),
        }
    kind, value = parsed

    if kind.upper() in _IDENTIFIER_PREFIXES:
        query_adapters = adapters
    else:
        adapter = _find_adapter_by_name(kind, adapters)
        if adapter is None:
            known = ", ".join(sorted(a.name for a in adapters)) or "(none supplied)"
            return {
                "status": STATUS_UNAVAILABLE,
                "reason": (
                    f"source_id names adapter {kind!r}, which is not among "
                    f"the adapters supplied to fetch_evidence() ({known}) -- "
                    "cannot re-query a source via an adapter that was not "
                    "provided."
                ),
            }
        query_adapters = [adapter]

    candidates, errors_by_adapter = _search_all(value, query_adapters)

    matching = [c for c in candidates if _matches_source_id(c, kind, value)]

    if not matching:
        if errors_by_adapter and not candidates:
            by_adapter = ", ".join(
                f"{name}={err.state}" for name, err in errors_by_adapter.items()
            )
            reason = (
                f"Re-querying source_id {source_id!r} failed on every "
                f"adapter tried ({by_adapter}) -- the source may have "
                "since become unavailable, or this is a transient "
                "transport error."
            )
        else:
            reason = (
                f"Re-querying source_id {source_id!r} returned no record "
                "whose own identifier exactly matches -- this source "
                "cannot currently be re-resolved to real evidence (it may "
                "since have been removed, renamed, or the id may be stale)."
            )
        return {"status": STATUS_UNAVAILABLE, "reason": reason}

    # Prefer the candidate with abstract text, if more than one real record
    # matched (e.g. a DOI-driven search hitting the same work via more than
    # one adapter) -- never a preference for which one to REPORT, only for
    # which real match gives the richer, still 100% real, evidence level.
    matching.sort(key=lambda c: bool(c.abstract and c.abstract.strip()), reverse=True)
    best = matching[0]

    evidence_text = (best.abstract or "").strip()
    if evidence_text:
        evidence_level = EVIDENCE_LEVEL_ABSTRACT
        locator = LOCATOR_ABSTRACT
    else:
        evidence_level = EVIDENCE_LEVEL_METADATA
        locator = LOCATOR_METADATA_ONLY

    return {
        "status": STATUS_OK,
        "source_id": source_id,
        "evidence_text": evidence_text,
        "evidence_level": evidence_level,
        "locator": locator,
        "metadata": {
            "title": best.title,
            "authors": list(best.authors),
            "year": best.year,
            "doi": best.doi,
            "pmid": best.pmid,
            "source_adapter": best.source_adapter,
            "source_record_id": best.source_record_id,
            "url": best.url,
        },
        "fetched_at": time.time(),
        "source_retrieved_at": best.retrieved_at,
    }
