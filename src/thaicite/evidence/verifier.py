"""Evidence gate: G1-G7 (subset appropriate for a v0.1 prototype).

A CanonicalWork only becomes VERIFIED if ALL gates pass. Any gate failure
leaves the work in its current state (never silently promoted) and records
which gate(s) failed in `work.gate_results` and, on the first failure, sets
`work.state = REJECTED` with a reason so a caller can explain why.

Gates implemented:
  G1 source record exists            -> at least one candidate with a real source_record_id
  G2 identity resolved via real id   -> merged via can_merge() path, not semantic-only
  G3 title/authors/year consistent   -> present and (if multiple candidates) not conflicting
  G4 resolvable source URL/DOI       -> at least one candidate has a url or doi
  G5 abstract/metadata actually fetched -> at least one candidate has a non-empty abstract
                                           or non-empty raw_metadata (not assumed/invented)
  G6 candidate-vs-query bibliographic identity -> the returned candidate's own
                                           title/authors, compared against the
                                           QUERY CITATION STRING itself (never
                                           against free-text `context`), via a
                                           real token-set/edit-distance
                                           similarity measure plus
                                           author-surname overlap. A single
                                           shared generic word is never
                                           sufficient. REQUIRED -- a candidate
                                           that fails this must not reach
                                           VERIFIED even if every other gate
                                           passes.
  G7 no unresolved METADATA_CONFLICT -> work.state is not CONFLICT and work.conflicts is empty

Historical note (post-mortem, 2026-09-20 adversarial validation run,
tests/golden/CONCEPT_VALIDATION_REPORT.md): the original G6 compared
free-text `context` (or scenario-metadata prose) against a candidate's
title/abstract via weak single-keyword overlap, and NO gate anywhere in the
pipeline ever compared a returned candidate against the citation string
actually being verified. Combined with a stopword list missing common words
("this", "not", "one", "two", "work", "via", "where", "only", "real",
"same", "about", "adult", "into", "but", "them", "over", "source"), any
OpenAlex hit sharing even one generic word with the context was stamped
VERIFIED regardless of whether it was the work actually being cited
(POSITIVE_CONTROL: 15/15 FAIL). G6 below fixes this by checking identity
against the query instead; the old context-relevance signal is kept only as
non-gating informational output (`context_matched_keywords`), never as a
substitute for the identity check.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from thaicite.core.models import CanonicalWork, Decision, RelationLabel, VerificationState
from thaicite.normalize.tokenize import tokenize as _shared_tokenize
from thaicite.resolve.identity import bibliographic_exact_match, exact_identifier_match

# Common, low-information words that must never be enough on their own to
# link a candidate to a query or a context -- the original stopword list
# was missing exactly these, which is how single-shared-generic-word false
# positives slipped through (see module docstring / CONCEPT_VALIDATION_REPORT.md).
_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "in", "on", "for", "to", "with",
    "is", "are", "study", "analysis", "review",
    "this", "that", "not", "one", "two", "work", "works", "via", "where",
    "only", "real", "same", "about", "adult", "into", "but", "them", "over",
    "source", "all", "need", "well", "known", "based", "literature", "risk",
    "pair", "paper", "from", "was", "were", "has", "have", "had",
}


def _keywords(text: str) -> set[str]:
    # Tokenization itself (English regex vs. real Thai word segmentation)
    # is delegated to the shared helper in normalize/tokenize.py -- see its
    # module docstring for why the plain regex alone silently collapses an
    # unspaced Thai sentence into one token. Everything below (min length,
    # stopwords) is unchanged.
    tokens = _shared_tokenize(text)
    return {t for t in tokens if len(t) > 2 and t not in _STOPWORDS}


def _author_surname(name: str) -> str | None:
    name = (name or "").strip()
    if not name:
        return None
    if "," in name:
        surname = name.split(",", 1)[0]
    else:
        parts = name.split()
        surname = parts[-1] if parts else name
    surname = surname.strip().lower()
    return surname or None


def _author_surname_overlap(query: str, authors: list[str] | None) -> bool:
    """True iff any candidate author's surname literally appears in `query`.

    This is deliberately conservative (substring match on a normalized
    surname of meaningful length) -- it is one signal among several, never
    sufficient by itself unless combined with real title overlap (see
    gate_g6_identity_match).
    """
    if not authors or not query:
        return False
    q_lower = query.lower()
    for author in authors:
        surname = _author_surname(author)
        if surname and len(surname) > 2 and surname in q_lower:
            return True
    return False


def _title_token_overlap_ratio(query: str, title: str) -> tuple[float, set[str]]:
    """Token-set overlap between the QUERY CITATION and a candidate title.

    Normalized by the smaller of the two keyword sets, so a long citation
    string (which often embeds venue/year/authors on top of the title) is
    not diluted by its own length -- but a meaningful fraction of the
    smaller set must still overlap, never just one token.
    """
    q_kw = _keywords(query)
    t_kw = _keywords(title or "")
    if not q_kw or not t_kw:
        return 0.0, set()
    shared = q_kw & t_kw
    denom = min(len(q_kw), len(t_kw))
    ratio = len(shared) / denom if denom else 0.0
    return ratio, shared


def gate_g1_source_exists(work: CanonicalWork) -> bool:
    return any(c.source_adapter and c.source_record_id for c in work.candidates)


def gate_g2_identity_via_real_id(work: CanonicalWork) -> bool:
    """Real identifier resolution, never semantic-only.

    Passes for a singleton work too (one real source record is itself a
    real identifier: its own source_record_id) — g2 exists to block
    identity claims manufactured purely from a similarity score, not to
    require multi-source corroboration by itself (that is what raises
    confidence, not what this gate checks).
    """
    if len(work.candidates) == 1:
        return True
    primary = work.candidates[0]
    for other in work.candidates[1:]:
        if exact_identifier_match(primary, other) is None and not bibliographic_exact_match(
            primary, other
        ):
            # This pair was merged into the same CanonicalWork without a
            # real-identifier or bibliographic basis -- identity resolution
            # must never have produced this, but the gate double-checks.
            return False
    return True


def gate_g3_metadata_consistent(work: CanonicalWork) -> bool:
    primary = work.primary
    if not primary.title or not primary.title.strip():
        return False
    if not primary.authors:
        return False
    if primary.year is None:
        return False
    if work.conflicts:
        return False
    return True


def gate_g4_resolvable_source(work: CanonicalWork) -> bool:
    return any(c.doi or c.url for c in work.candidates)


def gate_g5_metadata_fetched(work: CanonicalWork) -> bool:
    return any((c.abstract and c.abstract.strip()) or c.raw_metadata for c in work.candidates)


def gate_g6_identity_match(work: CanonicalWork, query: str) -> tuple[bool, dict]:
    """REQUIRED gate: does this candidate correspond to the citation being
    verified (`query`), not merely to the free-text `context`?

    Real bibliographic-identity check: normalized token-set overlap on the
    TITLE specifically (candidate title vs. the query citation string
    itself) plus author-surname overlap where authors are available. A
    single shared generic word is never enough to pass -- at least two
    real shared title tokens are required unless a genuine author-surname
    match is also present.
    """
    primary = work.primary
    title_ratio, shared_tokens = _title_token_overlap_ratio(query, primary.title or "")
    seq_ratio = SequenceMatcher(
        None, " ".join(sorted(_keywords(query))), " ".join(sorted(_keywords(primary.title or "")))
    ).ratio()
    author_match = _author_surname_overlap(query, primary.authors)

    debug = {
        "shared_title_tokens": sorted(shared_tokens),
        "title_token_ratio": round(title_ratio, 3),
        "title_seq_ratio": round(seq_ratio, 3),
        "author_surname_match": author_match,
    }

    if not query or not query.strip():
        # No query citation string to compare against at all (a caller
        # invoked the pipeline with only free-text context) -- identity
        # cannot be checked here. Fail closed rather than silently passing,
        # since VERIFIED must never be reached without a real identity
        # check having actually run.
        debug["reason"] = "no_query_to_compare_against"
        return False, debug

    # A lone coincidental shared generic word is never sufficient, with or
    # without a similarity ratio clearing threshold on a short title.
    if len(shared_tokens) < 2 and not author_match:
        debug["reason"] = "fewer_than_2_shared_tokens_and_no_author_match"
        return False, debug

    passed = (
        title_ratio >= 0.5
        or seq_ratio >= 0.55
        or (author_match and len(shared_tokens) >= 1)
    )
    return passed, debug


def gate_g6b_context_relevance_signal(work: CanonicalWork, context: str) -> tuple[bool, list[str]]:
    """Non-gating informational signal only -- kept from the pre-fix G6 for
    display/debugging value, but it NEVER substitutes for
    `gate_g6_identity_match` and never affects work.state on its own; it is
    not part of the `all(...)` check in `verify()` when `mode="identity"`
    (the default -- see `verify()`). Free-text context can legitimately
    share vocabulary with an unrelated work, so this alone must never be
    read as "this is the cited work" under identity mode.

    Under `mode="discovery"` this SAME signal is the basis (strengthened by
    `gate_g6_discovery_relevance` below) for an actual gate -- discovery is
    answering a different question ("is this real, on-topic work relevant
    to this broad context?"), where topical overlap is exactly the right
    signal, not a weak substitute for one.
    """
    if not context or not context.strip():
        return True, []
    ctx_kw = _keywords(context)
    primary = work.primary
    doc_text = " ".join(filter(None, [primary.title, primary.abstract or ""]))
    doc_kw = _keywords(doc_text)
    matched = sorted(ctx_kw & doc_kw)
    return (len(matched) > 0, matched)


# Same "a single shared generic word is never enough" floor as
# gate_g6_identity_match's `_MIN_SHARED_FOR_DIRECTIONAL`-style threshold,
# applied to discovery-mode topical relevance instead of citation identity.
_MIN_SHARED_FOR_DISCOVERY_RELEVANCE = 2

# Bridge signal ONLY for discovery-mode relevance (never used by G6 identity
# matching, never used by classify_relation): `_keywords()`'s word-boundary
# regex treats an unspaced run of Thai script as ONE token (Thai does not
# use inter-word spaces the way English does), so two genuinely on-topic
# Thai sentences that happen to be written without internal spaces (a real,
# common case -- e.g. a discovery context typed as one unbroken phrase) can
# share zero whole-run tokens even though they share real vocabulary. A
# proper Thai word segmenter is a separate, sibling fix (see
# evidence/relation.py's own module docstring / this repo's Thai-tokenizer
# workstream) that this gate does not attempt to duplicate; in the
# meantime, this character n-gram overlap is a scoped fallback SIGNAL, used
# ONLY here, that still requires a real minimum shared-substring count (not
# a single coincidental short match) before counting as relevant.
_THAI_NGRAM_LEN = 4
_MIN_SHARED_THAI_NGRAMS = 3
_THAI_SCRIPT_RE = re.compile(r"[฀-๿]")


def _thai_char_ngrams(text: str, n: int = _THAI_NGRAM_LEN) -> set[str]:
    thai_only = "".join(ch for ch in (text or "") if _THAI_SCRIPT_RE.match(ch))
    if len(thai_only) < n:
        return set()
    return {thai_only[i : i + n] for i in range(len(thai_only) - n + 1)}


def _thai_ngram_overlap(context: str, doc_text: str) -> set[str]:
    ctx_ngrams = _thai_char_ngrams(context)
    doc_ngrams = _thai_char_ngrams(doc_text)
    return ctx_ngrams & doc_ngrams


def gate_g6_discovery_relevance(work: CanonicalWork, context: str) -> tuple[bool, dict]:
    """DISCOVERY-MODE relevance gate (`verify(..., mode="discovery")` /
    `core.engine.discover_citations()`), used IN PLACE OF
    `gate_g6_identity_match` -- never alongside it as an additional
    requirement, and NEVER used by `verify_cite()`'s identity path.

    Answers "is this real candidate topically relevant to this broad
    discovery CONTEXT?" -- not "is this candidate bibliographically the
    SAME work as this exact citation string?" (that is
    `gate_g6_identity_match`'s job, for `verify_cite()`'s identity path).
    Built on the same topical-overlap signal
    `gate_g6b_context_relevance_signal` already computes for informational
    display purposes in identity mode; here that signal is strengthened
    slightly (at least 2 shared non-stopword keywords, matching G6's own
    "one generic shared word is never sufficient" floor) and actually
    gates `work.state` when discovery mode is active.

    A context with fewer than 2 real (non-stopword) keywords of its own
    (e.g. a two-word topic phrase) cannot demand 2 shared keywords back --
    a single real shared keyword is accepted in that case, since discovery
    is meant to be lenient about bibliographic IDENTITY, not about being
    entirely unrelated to the topic.

    This function reuses whatever tokenization `_keywords()`/
    `gate_g6b_context_relevance_signal()` currently implement (including
    any Thai-segmentation improvements made elsewhere in this module) --
    it adds no tokenization logic of its own.
    """
    signal, matched = gate_g6b_context_relevance_signal(work, context)
    ctx_kw = _keywords(context or "")
    debug = {
        "matched_context_keywords": matched,
        "context_keyword_count": len(ctx_kw),
    }
    if not context or not context.strip():
        debug["reason"] = "no_context_to_compare_against"
        return False, debug

    required = (
        _MIN_SHARED_FOR_DISCOVERY_RELEVANCE
        if len(ctx_kw) >= _MIN_SHARED_FOR_DISCOVERY_RELEVANCE
        else 1
    )
    passed = len(matched) >= required
    debug["reason"] = (
        "sufficient_topical_overlap" if passed else "insufficient_topical_overlap"
    )
    debug["required_shared_keywords"] = required

    if not passed:
        # Thai n-gram bridge fallback (see module comment above
        # `_THAI_NGRAM_LEN`) -- only consulted when the word-token check
        # above did not already pass, and only ever used to ADD relevance
        # evidence, never to subtract it.
        primary = work.primary
        doc_text = " ".join(filter(None, [primary.title, primary.abstract or ""]))
        shared_ngrams = _thai_ngram_overlap(context, doc_text)
        debug["shared_thai_ngrams"] = len(shared_ngrams)
        if len(shared_ngrams) >= _MIN_SHARED_THAI_NGRAMS:
            passed = True
            debug["reason"] = "sufficient_thai_ngram_overlap"

    return passed, debug


def gate_g7_no_conflict(work: CanonicalWork) -> bool:
    return work.state != VerificationState.CONFLICT and not work.conflicts


# `verify()`/`gate_admission_decision()` mode tags. IDENTITY is the default
# and the ONLY mode `verify_cite()` (mcp_server.py) ever uses -- a caller
# supplying an actual specific citation string to check candidate identity
# against. DISCOVERY is used ONLY by `core.engine.discover_citations()`
# (find_cites()/`thaicite find`'s broad-topic path) and must never be the
# default, so a caller that forgets to pass `mode` explicitly always gets
# the strict, pre-existing identity-matching behavior unchanged.
VERIFY_MODE_IDENTITY = "identity"
VERIFY_MODE_DISCOVERY = "discovery"
VERIFY_MODES = frozenset({VERIFY_MODE_IDENTITY, VERIFY_MODE_DISCOVERY})


def verify(
    work: CanonicalWork,
    context: str = "",
    query: str = "",
    mode: str = VERIFY_MODE_IDENTITY,
) -> tuple[CanonicalWork, list[str]]:
    """Run all gates against `work`. Returns (work, context_matched_keywords).

    `query` is the actual citation string being verified (what the caller
    is trying to confirm) -- under `mode="identity"` (the default,
    unchanged from before) this is what the REQUIRED G6 identity gate
    checks the candidate against, and `context` is free-text surrounding
    prose used only for the non-gating G6b informational signal.

    Under `mode="discovery"`, the REQUIRED gate in G6's slot is
    `gate_g6_discovery_relevance(work, context)` instead --
    `gate_g6_identity_match` is still computed and recorded (in
    `work.gate_results["G6_identity_match"]` and `work.g6_identity_debug`)
    for transparency/debugging, but it does NOT gate `work.state` in this
    mode; a real, on-topic candidate is never rejected here just because
    its title does not share 2+ literal tokens with a broad topic phrase
    that was never meant to BE a citation string. See module docstring and
    `core.engine.discover_citations()`.

    On all-pass (for the active mode's required gate set), sets
    work.state = VERIFIED. On any failure, sets work.state = REJECTED
    (unless it was already CONFLICT, which is a more specific and more
    informative state -- CONFLICT is left as-is so the caller can see *why*
    it was rejected). `work.gate_results` always records the per-gate
    pass/fail so a caller never has to guess which gate blocked
    verification.
    """
    if mode not in VERIFY_MODES:
        raise ValueError(f"Unknown verify() mode: {mode!r}, expected one of {sorted(VERIFY_MODES)}")

    g1 = gate_g1_source_exists(work)
    g2 = gate_g2_identity_via_real_id(work)
    g3 = gate_g3_metadata_consistent(work)
    g4 = gate_g4_resolvable_source(work)
    g5 = gate_g5_metadata_fetched(work)
    g6_identity, g6_identity_debug = gate_g6_identity_match(work, query)
    g6b_signal, matched_keywords = gate_g6b_context_relevance_signal(work, context)
    g6_discovery, g6_discovery_debug = gate_g6_discovery_relevance(work, context)
    g7 = gate_g7_no_conflict(work)

    g6_required = g6_discovery if mode == VERIFY_MODE_DISCOVERY else g6_identity

    work.gate_results = {
        "G1_source_exists": g1,
        "G2_identity_real": g2,
        "G3_metadata_consistent": g3,
        "G4_resolvable_source": g4,
        "G5_metadata_fetched": g5,
        # Identity check: REQUIRED and gating in identity mode; recorded
        # for transparency only (never gates work.state) in discovery mode.
        "G6_identity_match": g6_identity,
        # Topical-relevance check: REQUIRED and gating in discovery mode
        # only; recorded for transparency in identity mode (where G6b above
        # already serves this informational purpose).
        "G6_discovery_relevance": g6_discovery,
        "G6b_context_relevance_signal_nongating": g6b_signal,
        "G7_no_conflict": g7,
    }
    # Debug evidence for both G6 variants (title-overlap ratios, shared
    # tokens, author-surname match / matched context keywords) is kept
    # separately from gate_results -- gate_results stays a pure bool map so
    # callers/tests iterating it for pass/fail never trip over a non-bool
    # value.
    work.g6_identity_debug = {
        "mode": mode,
        "identity": g6_identity_debug,
        "discovery_relevance": g6_discovery_debug,
    }

    # G6b (context relevance) is deliberately NOT in this all(...) check --
    # it is informational only, per the module docstring and post-mortem.
    # Only the mode-appropriate G6 variant (g6_required) gates work.state.
    if all((g1, g2, g3, g4, g5, g6_required, g7)):
        work.state = VerificationState.VERIFIED
    elif work.state != VerificationState.CONFLICT:
        work.state = VerificationState.REJECTED

    return work, matched_keywords


def gate_admission_decision(
    work: CanonicalWork,
    relation: str,
    intended_relation: str = RelationLabel.SUPPORTS,
    mode: str = VERIFY_MODE_IDENTITY,
) -> tuple[str, dict]:
    """Deterministic 3-way ADMIT/REJECT/HOLD gate (ARCHITECTURE.md SS89,
    SS98) -- an additional outcome layer on top of `verify()`'s existing
    G1-G7 state machine, never a replacement for it. This function does
    not mutate `work` and does not re-run G1-G7; it only READS
    `work.state` / `work.gate_results` (already set by `verify()`) plus a
    `relation` label (from `evidence/relation.py::classify_relation` or a
    caller-substituted classifier) and decides.

    `relation` never sets the decision on its own -- role separation
    (SS98): the API layer produced `work.state`/`work.gate_results`, an
    LLM-or-heuristic layer produced `relation`, and only THIS function,
    a plain deterministic mapping, produces `decision`.

    Mapping (ARCHITECTURE.md SS89 examples preserved verbatim):
      VERIFIED + relation matches the intended use          -> ADMIT
      VERIFIED + relation is a real directional mismatch
        (e.g. relation=CHALLENGES when intended=SUPPORTS)   -> REJECT
      VERIFIED + relation UNCLEAR or CONTEXT_ONLY            -> HOLD
      work.state CONFLICT (unresolved METADATA_CONFLICT)     -> HOLD
      work.state in ERROR_STATES (RATE_LIMITED/TIMEOUT/
        ACCESS_DENIED/PARSER_ERROR -- paywall-like/transport) -> HOLD
      work.state NOT_FOUND                                   -> HOLD
      work.state REJECTED, G6_identity_match failed           -> REJECT
        (a real contradiction: this candidate is not even the
        work being cited)
      work.state REJECTED, G5_metadata_fetched failed          -> HOLD
        (content was never actually obtained -- insufficient
        access/resolution to decide, not evidence of a mismatch)
      work.state REJECTED, any other gate failed                -> REJECT
      any earlier pipeline state (DISCOVERED/IDENTIFIED/
        METADATA_VERIFIED/CONTENT_FETCHED/CONTEXT_MATCHED)
        with a clear relation already available                  -> ADMIT
        (per SS89: "CONTENT_FETCHED-with-clear-relation -> ADMIT")
      any earlier pipeline state otherwise                        -> HOLD

    Returns:
        (decision, debug) where `decision` is one of
        `Decision.ADMIT/REJECT/HOLD` and `debug` is a plain dict recording
        which branch of the mapping fired, for callers/tests to inspect
        without re-deriving it.
    """
    debug: dict = {
        "work_state": work.state,
        "relation": relation,
        "intended_relation": intended_relation,
    }

    if work.state == VerificationState.CONFLICT:
        debug["reason"] = "unresolved_metadata_conflict"
        return Decision.HOLD, debug

    if work.state in VerificationState.ERROR_STATES:
        debug["reason"] = f"access_or_transport_error:{work.state}"
        return Decision.HOLD, debug

    if work.state == VerificationState.NOT_FOUND:
        debug["reason"] = "not_found"
        return Decision.HOLD, debug

    if work.state == VerificationState.REJECTED:
        # Which G6 variant actually gated this work into REJECTED depends
        # on `mode` -- see `verify()`. Using the wrong one here would read
        # a purely-informational gate (never gating in this mode) as if it
        # were the reason for the real mismatch.
        mismatch_gate = (
            "G6_discovery_relevance" if mode == VERIFY_MODE_DISCOVERY else "G6_identity_match"
        )
        g6_passed = work.gate_results.get(mismatch_gate)
        g5_passed = work.gate_results.get("G5_metadata_fetched")
        if g6_passed is False:
            debug["reason"] = f"{mismatch_gate}_failed_real_mismatch"
            return Decision.REJECT, debug
        if g5_passed is False:
            debug["reason"] = "G5_metadata_not_fetched_insufficient_access"
            return Decision.HOLD, debug
        failed_gates = [name for name, passed in work.gate_results.items() if not passed]
        debug["reason"] = f"failed_gate(s):{','.join(failed_gates) or 'unknown'}"
        return Decision.REJECT, debug

    directional_and_clear = (
        relation in (RelationLabel.SUPPORTS, RelationLabel.CHALLENGES)
    )

    if work.state in (VerificationState.VERIFIED, VerificationState.CONTENT_FETCHED):
        if not directional_and_clear:
            debug["reason"] = f"relation_{relation}_not_directional"
            return Decision.HOLD, debug
        if relation == intended_relation:
            debug["reason"] = "relation_matches_intended_use"
            return Decision.ADMIT, debug
        debug["reason"] = (
            f"relation_{relation}_does_not_match_intended_{intended_relation}"
        )
        return Decision.REJECT, debug

    # Any other pipeline-in-progress state (DISCOVERED/IDENTIFIED/
    # METADATA_VERIFIED/CONTEXT_MATCHED) -- not enough has happened yet.
    debug["reason"] = f"insufficient_pipeline_state:{work.state}"
    return Decision.HOLD, debug
