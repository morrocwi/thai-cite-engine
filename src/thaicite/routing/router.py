"""Source Router: query/context -> domain -> ordered adapter list.

Described in prose in ARCHITECTURE.md §6 ("Domain Router"), §56 ("Reduced
Source Router -- 4 core adapters for v1") and §67/§71 ("Thailand-first
federation" as the project's stated #1 differentiator). This module is the
first real implementation.

Two jobs only, matching the AI-boundary wall in ARCHITECTURE.md §57
("domain detection" is an AI-allowed suggestion, never a citation record):

  1. Classify a (context, query) pair into a domain -- GENERAL, THAI,
     HEALTH, or THAI_HEALTH for v1 -- with a deterministic keyword/script
     heuristic. This is classification/routing metadata, exactly like
     `normalize/thai_relevance.py`'s tags -- it never admits or rejects a
     citation itself; that stays the job of `evidence/verifier.py`'s G1-G7
     gates, run downstream inside `core.engine.resolve_citations()`.
  2. Decide WHICH adapters to pass into `resolve_citations()`, and in what
     priority order, given the adapters the caller actually has configured
     (`available_adapters`). `resolve_citations()` itself already
     aggregates across every adapter it is given and isolates a failing
     adapter's error from the others (see core/engine.py's `errors_by_adapter`
     handling) -- this router never calls an adapter itself and never
     blocks on one; it only builds the list and the priority order.

Founder instruction (explicit, CRITICAL, carried verbatim into this
module's design): **Thai-source adapters (ThaiJO) are queried FIRST by
default priority, not as a fallback after global sources** -- this is the
project's own stated #1 differentiator (ARCHITECTURE.md §67/§71,
"Thailand-first federation"), not a minor detail. Concretely:

  - THAI and THAI_HEALTH domains: ThaiJO precedes OpenAlex/Crossref/PubMed.
  - GENERAL domain: ThaiJO is STILL included (not skipped), and still
    placed first, precisely so a query that carries no positive signal
    either way defaults toward including the Thai track rather than
    excluding it. There is deliberately no "detected as definitely
    non-Thai" heuristic in this module for v1 -- absence of a Thai signal
    is not treated as a signal of non-Thai-relevance.
  - HEALTH domain (health signal present, no Thai signal at all) is the
    one case that follows ARCHITECTURE.md §6's table as written --
    PubMed/PMC first, then the global adapters, no ThaiJO -- because a
    real Thai or health-domain signal was actively checked for and only
    the Thai one came back negative; this is the narrow, deliberate
    exception, not evidence the "err toward including Thai" default is
    being walked back elsewhere.

Founder instruction (explicit, CRITICAL): **automatic, graceful failover --
if one adapter hits RATE_LIMITED/TIMEOUT/ACCESS_DENIED/PARSER_ERROR, the
router must never stall waiting on it.** `resolve_citations()` already
provides that isolation at the adapter-call level. This module's added
responsibility is honest, non-silent surfacing: `RouteDecision.track_status`
starts as a per-track ("global" vs "local") expectation based on which
adapters were actually routed in, and `RouteDecision.update_track_status()`
is called by the caller *after* `resolve_citations()` returns, to fold the
real per-adapter error/success evidence back in -- so a track that came
back "healthy-looking" only because a sibling adapter covered for a failing
one is still reported as degraded, never silently identical to a fully
healthy track.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from thaicite.adapters.base import SourceAdapter
from thaicite.core.models import VerificationState
from thaicite.normalize.thai_relevance import (
    ABOUT_THAILAND,
    PUBLISHED_IN_THAILAND,
    THAI_LANGUAGE,
    classify_thai_relevance,
)

# --------------------------------------------------------------- Domains --

GENERAL = "GENERAL"
THAI = "THAI"
HEALTH = "HEALTH"
THAI_HEALTH = "THAI_HEALTH"

ALL_DOMAINS = frozenset({GENERAL, THAI, HEALTH, THAI_HEALTH})

# ------------------------------------------------------- Adapter tracks --

# Canonical adapter names, matching each real adapter class's `.name`
# (adapters/openalex.py, crossref.py, thaijo.py, pubmed.py). Keeping this
# as the single source of truth for "which track an adapter belongs to"
# avoids re-deriving it ad hoc anywhere track_status is computed.
LOCAL_TRACK_ADAPTERS = frozenset({"THAIJO"})  # TNRR/TCI/TDC join this later
GLOBAL_TRACK_ADAPTERS = frozenset({"OPENALEX", "CROSSREF", "PUBMED"})

TRACK_GLOBAL = "global"
TRACK_LOCAL = "local"

# Track status values `RouteDecision.track_status` can hold.
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_NOT_QUERIED = "not_queried"

# ------------------------------------------------- Per-domain adapter order --

# ARCHITECTURE.md §6/§56 core adapter names, ordered Thai-first per the
# founder instruction above. GENERAL and THAI both start with ThaiJO
# (included by default, not a fallback); HEALTH is the one domain that
# follows §6's table as written (no positive Thai signal was found).
_DOMAIN_ADAPTER_ORDER: dict[str, tuple[str, ...]] = {
    GENERAL: ("THAIJO", "OPENALEX", "CROSSREF"),
    THAI: ("THAIJO", "OPENALEX", "CROSSREF"),
    HEALTH: ("PUBMED", "OPENALEX", "CROSSREF"),
    THAI_HEALTH: ("THAIJO", "PUBMED", "OPENALEX", "CROSSREF"),
}

# ------------------------------------------------------- Health heuristic --

# Deterministic keyword heuristic, English + Thai, adequate for v1 (same
# "keyword-list based, adequate for this prototype" standard already used
# by normalize/thai_relevance.py's `_THAILAND_KEYWORDS`).
_HEALTH_KEYWORDS = {
    "health", "medicine", "medical", "clinical", "disease", "patient",
    "patients", "treatment", "therapy", "diagnosis", "diagnostic",
    "epidemiology", "epidemiological", "hospital", "nursing", "nurse",
    "pharmacology", "pharmaceutical", "vaccine", "vaccination", "surgery",
    "surgical", "cancer", "oncology", "cardiology", "cardiovascular",
    "diabetes", "infection", "infectious", "public health", "healthcare",
    "biomedical", "who guideline", "clinical trial", "morbidity",
    "mortality", "pathology", "pediatric", "psychiatry", "psychiatric",
    "mental health",
    # Thai health terms.
    "สุขภาพ", "การแพทย์", "แพทย์", "โรค", "ผู้ป่วย", "การรักษา", "รักษา",
    "ยา", "พยาบาล", "โรงพยาบาล", "สาธารณสุข", "วัคซีน", "ผ่าตัด", "มะเร็ง",
    "เบาหวาน", "โรคติดเชื้อ", "ระบาดวิทยา", "คลินิก",
}

_WORD_RE = re.compile(r"[a-zA-Z]+|[ก-๙]+")


def _has_health_signal(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    # Multi-word English phrases: substring match is enough (no false
    # positive risk worth guarding against at this heuristic's precision).
    for keyword in _HEALTH_KEYWORDS:
        if " " in keyword or not keyword.isascii():
            if keyword in lowered:
                return True
    # Single ASCII words: match on tokens so "healthcare" doesn't
    # accidentally get credit from a stray "health" substring inside an
    # unrelated longer word (kept deterministic, still just a heuristic).
    tokens = {tok.lower() for tok in _WORD_RE.findall(text)}
    return bool(tokens & {k for k in _HEALTH_KEYWORDS if k.isascii() and " " not in k})


def _has_thai_signal(text: str) -> bool:
    """Reuse `normalize/thai_relevance.py` rather than re-implementing it.

    `classify_thai_relevance` expects a `Candidate`-shaped object
    (`.title`/`.abstract`/`.url`/`.raw_metadata`); a bare piece of routing
    text has no venue/host metadata to check, so it is passed in as the
    `title` field and the other fields are left empty. This still exercises
    the same THAI_LANGUAGE (Thai-script ratio) and ABOUT_THAILAND
    (Thailand/region keyword) checks that module already implements, without
    duplicating that logic here. PUBLISHED_IN_THAILAND legitimately can't
    fire on bare text (no host/venue metadata exists yet at routing time),
    which is expected and fine -- it is one signal, not required for a
    "Thai" classification.
    """
    if not text:
        return False
    pseudo_candidate = SimpleNamespace(
        title=text, abstract="", url=None, raw_metadata={}
    )
    tags = classify_thai_relevance(pseudo_candidate)
    return bool(
        {THAI_LANGUAGE, PUBLISHED_IN_THAILAND, ABOUT_THAILAND} & set(tags)
    )


def classify_domain(context: str, query: str) -> str:
    """Classify `(context, query)` into GENERAL/THAI/HEALTH/THAI_HEALTH.

    Deterministic, keyword/script-based -- adequate for v1 (ARCHITECTURE.md
    §6/§57: "domain detection" is a classification suggestion, never an
    admission gate). Both `context` and `query` are checked together so a
    Thai-language claim searched with an English query string (or vice
    versa) still gets a Thai signal.
    """
    text = f"{context or ''} {query or ''}"
    is_thai = _has_thai_signal(text)
    is_health = _has_health_signal(text)
    if is_thai and is_health:
        return THAI_HEALTH
    if is_health:
        return HEALTH
    if is_thai:
        return THAI
    return GENERAL


# ------------------------------------------------------------ RouteDecision --


@dataclass
class RouteDecision:
    """Router output: domain + ordered adapter list + honest track health.

    `adapters` is what a caller passes straight into
    `core.engine.resolve_citations(context, queries, adapters)`.

    `track_status` starts as this router's *a priori* expectation --
    STATUS_OK for a track with at least one adapter routed in,
    STATUS_NOT_QUERIED for a track this domain didn't route to at all (e.g.
    HEALTH routes no local-track adapter). It is deliberately NOT a claim
    about live adapter health yet -- the router itself never calls an
    adapter. Call `update_track_status()` with `resolve_citations()`'s
    return value once that call has actually happened, to fold in the real
    per-adapter evidence.
    """

    domain: str
    adapters: list[SourceAdapter] = field(default_factory=list)
    adapter_names: list[str] = field(default_factory=list)
    track_status: dict[str, str] = field(default_factory=dict)
    # Debug/transparency only -- never re-consumed as a gate, same
    # discipline as Candidate.thai_relevance (normalize/thai_relevance.py).
    signals: dict[str, Any] = field(default_factory=dict)

    def update_track_status(self, engine_result: dict[str, Any]) -> dict[str, str]:
        """Fold `resolve_citations()`'s return value back into `track_status`.

        Honest-degradation rule (founder instruction): a track is reported
        STATUS_DEGRADED the moment *any* adapter routed into it is seen
        reporting a real transport error (RATE_LIMITED/TIMEOUT/
        ACCESS_DENIED/PARSER_ERROR) anywhere in this result -- even if a
        sibling adapter on the same track still produced usable results.
        Silently downgrading "one adapter failed, another covered for it"
        to "ok" would hide exactly the signal this rule exists to surface.

        Evidence sources read from `engine_result` (no change to
        `core/engine.py` required):
          - `rejected["query::<q>"]["by_adapter"]` and
            `not_found_queries["<q>"]["by_adapter"]` -- populated only for
            a query where *every* adapter came back empty/erroring
            (core/engine.py's `resolve_citations`), but any real error
            state appearing there for an adapter is still real evidence
            that adapter is unhealthy.
          - `verified` / other `rejected` entries -- each references a
            `CanonicalWork` whose candidates carry `source_adapter`; an
            adapter that contributed at least one candidate anywhere is
            evidence that adapter is currently reachable.
        """
        errored_adapters: set[str] = set()
        healthy_adapters: set[str] = set()

        for bucket_key in ("rejected", "not_found_queries"):
            bucket = engine_result.get(bucket_key) or {}
            for entry in bucket.values():
                if not isinstance(entry, dict):
                    continue
                by_adapter = entry.get("by_adapter") or {}
                for adapter_name, info in by_adapter.items():
                    state = info.get("state") if isinstance(info, dict) else None
                    if state in VerificationState.ERROR_STATES:
                        errored_adapters.add(adapter_name)

        for citation in engine_result.get("verified") or []:
            for candidate in getattr(citation.work, "candidates", []):
                name = getattr(candidate, "source_adapter", None)
                if name:
                    healthy_adapters.add(name)

        rejected = engine_result.get("rejected") or {}
        for key, entry in rejected.items():
            if not isinstance(entry, dict) or entry.get("reason") == "adapter_error":
                continue
            # key is "<source_adapter>:<source_record_id>" for a real
            # (non-transport-error) rejection -- that adapter reached and
            # returned a real record, so it is evidence of health too.
            adapter_name = key.split(":", 1)[0]
            if adapter_name:
                healthy_adapters.add(adapter_name)

        for track, track_adapter_names in (
            (TRACK_GLOBAL, GLOBAL_TRACK_ADAPTERS),
            (TRACK_LOCAL, LOCAL_TRACK_ADAPTERS),
        ):
            routed = {
                name for name in self.adapter_names if name in track_adapter_names
            }
            if not routed:
                continue  # leave whatever this router already set (not_queried)
            if routed & errored_adapters:
                self.track_status[track] = STATUS_DEGRADED
            elif routed & healthy_adapters:
                self.track_status[track] = STATUS_OK
            # else: routed but no evidence either way yet -- leave as-is
            # (the a-priori STATUS_OK from route()) rather than guessing.

        return self.track_status


# ------------------------------------------------------------------ route --


def route(
    context: str,
    query: str,
    available_adapters: list[SourceAdapter],
) -> RouteDecision:
    """Classify `(context, query)` and build the ordered adapter list.

    `available_adapters` is whatever the caller has actually constructed/
    configured (e.g. `[OpenAlexAdapter(), CrossrefAdapter(), ThaiJOAdapter(),
    PubMedAdapter()]`) -- this router only selects and orders a subset of
    it, matching each adapter by its `.name`; it never constructs an
    adapter itself and never drops an adapter the domain's policy calls
    for just because it happens to be present, nor adds one the caller
    didn't supply.

    Returns a `RouteDecision` whose `.adapters` is ready to pass straight
    into `core.engine.resolve_citations(context, [query], adapters)`.
    """
    domain = classify_domain(context, query)
    by_name = {adapter.name: adapter for adapter in available_adapters}

    wanted_order = _DOMAIN_ADAPTER_ORDER[domain]
    ordered_names = [name for name in wanted_order if name in by_name]
    ordered_adapters = [by_name[name] for name in ordered_names]

    track_status: dict[str, str] = {}
    for track, track_adapter_names in (
        (TRACK_GLOBAL, GLOBAL_TRACK_ADAPTERS),
        (TRACK_LOCAL, LOCAL_TRACK_ADAPTERS),
    ):
        routed = [name for name in ordered_names if name in track_adapter_names]
        track_status[track] = STATUS_OK if routed else STATUS_NOT_QUERIED

    text = f"{context or ''} {query or ''}"
    signals = {
        "is_thai_signal": _has_thai_signal(text),
        "is_health_signal": _has_health_signal(text),
        "requested_but_unavailable": [
            name for name in wanted_order if name not in by_name
        ],
    }

    return RouteDecision(
        domain=domain,
        adapters=ordered_adapters,
        adapter_names=ordered_names,
        track_status=track_status,
        signals=signals,
    )
