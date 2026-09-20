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
from thaicite.evidence.relation import classify_relation
from thaicite.evidence.statement_type import (
    ALL_STATEMENT_TYPES,
    FINDING_STATEMENT_TYPES,
    StatementType,
    classify_statement_type,
)
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


def _normalize_identifier(value: str | None) -> str:
    """Normalize an identifier string for exact comparison: strip, lowercase,
    and drop a few common non-semantic wrapper prefixes (e.g. a caller
    pasting a full DOI URL instead of the bare DOI). This is NOT a fuzzy
    match -- it only removes formatting noise around an otherwise-exact
    identifier, never edits the identifier's own content.
    """
    v = (value or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:", "pmid:", "pmcid:"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    return v.strip()


def _exact_identifier_shortcut(work: CanonicalWork, query: str) -> tuple[bool, dict] | None:
    """PRIORITY-1 check for `gate_g6_identity_match`: does the (normalized)
    `query` string exactly match -- or exactly contain as a distinct
    identifier substring -- one of the candidate's own real identifier
    fields (doi/pmid/pmcid/source_record_id)?

    A real, exact identifier match (a caller citing "10.1038/171737a0" and
    the candidate's own `doi` field being exactly that) is strictly
    STRONGER evidence of identity than any fuzzy title/author overlap --
    it must never be treated as weaker just because it shares zero title
    tokens with a differently-worded title. When this fires, the fuzzy
    title/author matcher below is not even run for the identity decision.

    Returns `(True, debug)` on a match, or `None` when no identifier field
    lines up at all (in which case the caller falls through to the
    existing structured bibliographic match, unchanged).
    """
    q_norm = _normalize_identifier(query)
    if not q_norm:
        return None

    primary = work.primary
    id_fields = {
        "doi": primary.doi,
        "pmid": primary.pmid,
        "pmcid": primary.pmcid,
        "source_record_id": primary.source_record_id,
    }
    for field_name, field_value in id_fields.items():
        field_norm = _normalize_identifier(field_value)
        if not field_norm:
            continue
        # Exact match, or the normalized query contains the candidate's
        # normalized identifier as a DISTINCT, BOUNDARY-DELIMITED substring
        # (e.g. a citation string that embeds the bare DOI alongside other
        # text). A plain (boundary-unaware) substring check is NOT safe:
        # two different DOIs where one is a literal prefix of the other
        # (common in practice -- corrections/versions/same-issue neighbors
        # often share a DOI prefix, e.g. "10.1234/abcd" vs "10.1234/abcd2")
        # would otherwise falsely match. Require that the character
        # immediately before and after the match, if any, is not
        # alphanumeric -- i.e. the identifier is not itself a fragment of a
        # longer identifier string. Fixed 2026-09-20 after a live-reproduced
        # false positive: candidate.doi="10.1234/abcd" (unrelated title)
        # vs query="10.1234/abcd2" was wrongly accepted before this fix.
        if q_norm == field_norm:
            return True, {
                "reason": "exact_identifier_match",
                "matched_field": field_name,
                "matched_value": field_value,
            }
        if len(field_norm) >= 6:
            idx = q_norm.find(field_norm)
            if idx != -1:
                before_ok = idx == 0 or not q_norm[idx - 1].isalnum()
                after_pos = idx + len(field_norm)
                after_ok = after_pos == len(q_norm) or not q_norm[after_pos].isalnum()
                if before_ok and after_ok:
                    return True, {
                        "reason": "exact_identifier_match",
                        "matched_field": field_name,
                        "matched_value": field_value,
                    }
    return None


def gate_g6_identity_match(work: CanonicalWork, query: str) -> tuple[bool, dict]:
    """REQUIRED gate: does this candidate correspond to the citation being
    verified (`query`), not merely to the free-text `context`?

    Checked in priority order (2026-09-20 exact-identifier-shortcut fix):
      1. Exact identifier match -- `query`, once trimmed/normalized,
         exactly matches (or exactly contains as a distinct identifier
         substring) the candidate's own doi/pmid/pmcid/source_record_id.
         PASSES IMMEDIATELY; the fuzzy matcher below never runs. A real
         identifier match is strictly stronger evidence than fuzzy title
         overlap, so it must never be gated behind it or weakened by it.
      2. Otherwise, the existing structured bibliographic match: normalized
         token-set overlap on the TITLE specifically (candidate title vs.
         the query citation string itself) plus author-surname overlap
         where authors are available. A single shared generic word is
         never enough to pass -- at least two real shared title tokens are
         required unless a genuine author-surname match is also present.
    """
    if not query or not query.strip():
        # No query citation string to compare against at all (a caller
        # invoked the pipeline with only free-text context) -- identity
        # cannot be checked here. Fail closed rather than silently passing,
        # since VERIFIED must never be reached without a real identity
        # check having actually run.
        return False, {"reason": "no_query_to_compare_against"}

    exact_match = _exact_identifier_shortcut(work, query)
    if exact_match is not None:
        return exact_match

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
    statement_type: StatementType | None = None,
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

    `statement_type` (from `evidence/statement_type.py::
    classify_statement_type`, P0 fix 2026-09-20) is an INDEPENDENT,
    additional input that answers a different question than `relation`:
    not "does this passage's topic vocabulary support/challenge the
    claim", but "did this passage report a finding at all, or does it
    merely discuss/examine/hypothesize about the claim (OBJECTIVE,
    HYPOTHESIS, BACKGROUND, METHOD, LIMITATION, PRIOR_WORK, UNKNOWN)?"
    Only `RESULT` or `CONCLUSION` (`evidence.statement_type
    .FINDING_STATEMENT_TYPES`) may ever lead to ADMIT, REGARDLESS of what
    `relation` says -- a passage that merely discusses a claim can share
    every topic term the claim uses with no negation/reversal signal
    nearby, which `classify_relation()` alone would call SUPPORTS, even
    though no finding was ever reported (the confirmed bug this layer
    fixes). This check runs BEFORE/ALONGSIDE the relation-label check
    below, not instead of it: a RESULT/CONCLUSION passage still needs a
    real SUPPORTS relation to ADMIT. `statement_type` only ever NARROWS
    what could ADMIT, never widens it -- when it fails to clear the check
    it produces HOLD, not REJECT (no finding reported yet is a
    resolution/access limitation, not evidence of a contradiction), and
    a REJECTED `work.state` still reaches REJECT exactly as before this
    layer, whatever `statement_type` says. Passing `statement_type=None`
    (the default) skips this layer entirely, for callers that have not
    computed a statement type yet -- see `core/engine.py`'s call site for
    the only caller that supplies it end-to-end.

    Mapping (ARCHITECTURE.md SS89 examples preserved verbatim):
      VERIFIED + statement_type given and not RESULT/CONCLUSION -> HOLD
        (no finding reported to evaluate yet -- checked first)
      relation == QUALIFIES                                  -> HOLD
        (2026-09-20 round 4: the evidence affirms the claim's own
        direction under a narrower scope/condition/population than
        the claim states -- needs a human/caller decision, never an
        automatic ADMIT or REJECT; checked before the directional
        match/mismatch check below, for every state that could
        otherwise reach ADMIT)
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
        (per SS89: "CONTENT_FETCHED-with-clear-relation -> ADMIT",
        also subject to the statement_type check above)
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
        "statement_type": statement_type,
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

    # Statement-Type layer (P0 fix, see docstring above), the QUALIFIES
    # layer (round 4), and the directional match/mismatch mapping are all
    # shared with `check_claim_evidence()` (added for the AI-Reader /
    # deterministic-Checker role change, see that function's docstring) --
    # see `_admission_from_resolved_signals()` for the one place this
    # mapping is implemented, so it is never duplicated between the two
    # callers.
    return _admission_from_resolved_signals(
        work_state=work.state,
        relation=relation,
        intended_relation=intended_relation,
        statement_type=statement_type,
        debug=debug,
    )


def _admission_from_resolved_signals(
    work_state: str,
    relation: str,
    intended_relation: str,
    statement_type: StatementType | None,
    debug: dict,
) -> tuple[str, dict]:
    """Shared core of the ADMIT/REJECT/HOLD mapping (ARCHITECTURE.md SS89
    examples) once a `relation` and (optional) `statement_type` are already
    resolved -- used by both:

      - `gate_admission_decision()` (full G1-G7 pipeline; called only for a
        `work_state` that is NOT CONFLICT/an ERROR_STATE/NOT_FOUND/REJECTED
        -- those are handled by that function itself, before this helper
        runs, since they need `work.gate_results`, which this helper never
        sees).
      - `check_claim_evidence()` (claim<->evidence consistency check only,
        no `CanonicalWork` -- always calls this with
        `work_state=VerificationState.VERIFIED`, i.e. "assume identity/
        existence was already confirmed elsewhere via G1-G7; only decide
        whether THIS evidence text is admissible for THIS claim" -- see
        that function's own docstring for why it does not itself run
        G1-G7).

    Mutates and returns the caller's `debug` dict alongside the decision so
    both callers share one reason-trail format.
    """
    # Statement-Type layer (P0 fix): only a passage that actually reported
    # a finding (RESULT/CONCLUSION) may ever reach ADMIT, no matter what
    # `relation` says. This only ever narrows the outcome to HOLD, never
    # widens it, and never fires when `statement_type` was not supplied.
    if statement_type is not None and statement_type not in FINDING_STATEMENT_TYPES:
        debug["reason"] = (
            f"statement_type={statement_type}, no finding reported to evaluate"
        )
        return Decision.HOLD, debug

    # QUALIFIES layer (round 4, 2026-09-20): a narrower-scope-than-claimed
    # relation is never an automatic ADMIT or REJECT -- it needs a human/
    # caller decision about whether the narrower scope is acceptable for
    # this use. Checked before the directional match/mismatch logic below,
    # for every state that could otherwise reach ADMIT.
    if relation == RelationLabel.QUALIFIES:
        debug["reason"] = "relation_QUALIFIES_narrower_scope_than_claim_needs_human_decision"
        return Decision.HOLD, debug

    directional_and_clear = (
        relation in (RelationLabel.SUPPORTS, RelationLabel.CHALLENGES)
    )

    if work_state in (VerificationState.VERIFIED, VerificationState.CONTENT_FETCHED):
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
    debug["reason"] = f"insufficient_pipeline_state:{work_state}"
    return Decision.HOLD, debug


def check_claim_evidence(
    claim: str,
    passage: str,
    ai_statement_type: str | None = None,
    ai_relation: str | None = None,
    *,
    intended_relation: str = RelationLabel.SUPPORTS,
) -> dict:
    """AI-Reader-vs-deterministic-Checker consistency gate -- the "Cite
    Card" transparency record (2026-09-20, ThaiCite role-change fix).

    BACKGROUND (why this function exists, not another heuristic patch):
    four rounds of pattern-matching fixes to `classify_statement_type()`/
    `classify_relation()` each found the previous round insufficient --
    confirming that closed-vocabulary heuristics are the wrong tool for
    genuinely semantic judgment. The founder-approved fix is a role
    change, not another patch: ThaiCite is exposed via MCP to be called BY
    an AI agent, which does the SEMANTIC work (reading passages, judging
    statement type/relation) as a Scout+Reader role, while ThaiCite itself
    stays a lightweight, dependency-free deterministic tool. This function
    is the Gate half of that split: it ALWAYS runs the deterministic
    checker (`classify_statement_type`/`classify_relation`, unchanged) and
    treats an AI-proposed judgment as an input to check, never as truth to
    trust outright.

    Hard constraint honored here (founder decision, explicit, this
    session): no LLM API call is bundled into this function or anywhere
    else in `src/thaicite/` -- no `openai`/`anthropic` SDK import, no
    per-call vendor cost, no API-key requirement. `ai_statement_type`/
    `ai_relation` are plain string parameters this function CONSUMES; it
    never calls out to any AI itself.

    AI DISCOVERY CONTRACT (this is what the calling AI agent needs to
    know -- document it prominently, since this docstring is what that
    agent reads to know how to use this tool):
      AI (via the calling agent) MAY propose: a title hint, a DOI/PMID
      hint, search terms, a passage-relevance guess, its own read of
      statement type (`ai_statement_type`) or claim/evidence relation
      (`ai_relation`) for THIS (claim, passage) pair.
      AI MAY NOT claim, and no tool may accept as truth without
      independent checking: that a source is VERIFIED/CONFIRMED, that a
      citation is safe-to-cite, any bibliographic fact not backed by a
      real adapter record, or an ADMIT decision -- those only ever come
      from ThaiCite's own deterministic resolve/fetch/gate functions
      (this one included), never from what the AI asserts about its own
      proposal.

    Placement: alongside `gate_admission_decision()` in this module,
    because it reuses that function's exact admission mapping (via the
    shared `_admission_from_resolved_signals()` helper above) rather than
    reimplementing it, and because it is one more deterministic Gate
    function in the same family (G1-G7 existence/identity gates,
    `gate_admission_decision()`'s directional mapping, and now this
    claim<->evidence consistency check).

    Scope -- what this function does NOT do: it takes no `CanonicalWork`
    and does not run G1-G7 (source-exists / real-identifier / resolvable-
    URL / metadata-fetched -- see this module's own docstring). It answers
    ONLY "given this claim and this real, fetched `passage`, what
    statement type/relation does the passage show, and is that admissible
    for this claim" -- i.e. the same mapping `gate_admission_decision()`
    applies once a work has reached VERIFIED/content-available (see
    `_admission_from_resolved_signals()`, called here with
    `work_state=VerificationState.VERIFIED`). A caller that has not yet
    confirmed the source is real via `find_cites()`/`verify_cite()` (or an
    equivalent G1-G7 pass) must not treat this function's decision as a
    full citation verdict on its own -- reality-anchoring (does this
    source exist at all) is a separate, already-existing gate.

    Args:
        claim: the claim being checked against `passage`.
        passage: the real, fetched evidence text (title/abstract/passage
            -- never invented by the AI; must come from a real adapter
            record, e.g. via `find_cites()`/`verify_cite()`).
        ai_statement_type: the AI Reader's own proposed statement-type
            judgment for `passage` (one of
            `evidence.statement_type.ALL_STATEMENT_TYPES`'s values), or
            `None` if the caller is not participating in the AI-Reader
            role for this call (a pure deterministic-only caller, e.g.
            the CLI).
        ai_relation: the AI Reader's own proposed relation judgment (one
            of `RelationLabel.ALL`'s values), or `None`.
        intended_relation: same meaning as `gate_admission_decision()`'s
            parameter of the same name -- which direction the claim needs
            the evidence to point (default `RelationLabel.SUPPORTS`).

    Silent-value-rejection policy (explicit choice, documented per the
    task): an `ai_statement_type`/`ai_relation` that is not a legal value
    of its type is IGNORED -- treated exactly as if that argument had not
    been supplied at all -- rather than raising. An AI Reader's proposal
    is untrusted free text under the AI Discovery Contract above; a
    malformed proposal is exactly the class of input this checker must
    survive without crashing, not a caller bug worth an exception for.

    Returns:
        {
          "decision": "ADMIT" | "REJECT" | "HOLD",
          "reasons": [str, ...],   # every reason that contributed,
                                    # in order -- agreement/disagreement
                                    # findings first, then the admission
                                    # mapping's own reason.
          "ai_proposed": {"statement_type": ..., "relation": ...} | None,
                                    # None iff neither ai_statement_type
                                    # nor ai_relation was a legal value.
          "deterministic_checked": {
              "statement_type": checker_statement_type,
              "relation": checker_relation,
          },
          "agreement": true | false | None,
                                    # None -- no (valid) AI-proposed value
                                    # supplied at all (pure deterministic-
                                    # only caller -- falls back to
                                    # checker-only, EXACTLY matching
                                    # gate_admission_decision()'s existing
                                    # behavior byte-for-byte, per the
                                    # backward-compatibility requirement).
                                    # True -- every AI-proposed value
                                    # supplied agreed with the checker's
                                    # own independent classification.
                                    # False -- at least one AI-proposed
                                    # value DISAGREED with the checker --
                                    # `decision` is ALWAYS "HOLD" in this
                                    # case (disagreement is information,
                                    # never silently resolved by trusting
                                    # either side alone -- same discipline
                                    # as `evidence/relation.py`'s own
                                    # stage-1/stage-2 checker design).
        }
    """
    checker_statement_type = classify_statement_type(passage)
    checker_relation = classify_relation(passage, claim)

    # Silent-value-rejection: an illegal value is treated as not supplied.
    valid_ai_statement_type = (
        ai_statement_type if ai_statement_type in ALL_STATEMENT_TYPES else None
    )
    valid_ai_relation = ai_relation if ai_relation in RelationLabel.ALL else None

    ai_supplied = valid_ai_statement_type is not None or valid_ai_relation is not None

    reasons: list[str] = []
    disagreement = False

    if valid_ai_statement_type is not None:
        if valid_ai_statement_type == checker_statement_type:
            reasons.append(
                f"ai_statement_type={valid_ai_statement_type} agrees with "
                f"checker_statement_type={checker_statement_type}"
            )
        else:
            disagreement = True
            reasons.append(
                f"ai_statement_type={valid_ai_statement_type} vs "
                f"checker_statement_type={checker_statement_type} -- "
                "disagreement, routed to HOLD"
            )

    if valid_ai_relation is not None:
        if valid_ai_relation == checker_relation:
            reasons.append(
                f"ai_relation={valid_ai_relation} agrees with "
                f"checker_relation={checker_relation}"
            )
        else:
            disagreement = True
            reasons.append(
                f"ai_relation={valid_ai_relation} vs "
                f"checker_relation={checker_relation} -- disagreement, "
                "routed to HOLD"
            )

    ai_proposed = (
        {"statement_type": valid_ai_statement_type, "relation": valid_ai_relation}
        if ai_supplied
        else None
    )
    deterministic_checked = {
        "statement_type": checker_statement_type,
        "relation": checker_relation,
    }

    if disagreement:
        return {
            "decision": Decision.HOLD,
            "reasons": reasons,
            "ai_proposed": ai_proposed,
            "deterministic_checked": deterministic_checked,
            "agreement": False,
        }

    # No disagreement: either no AI value was supplied at all (falls back
    # to the checker's own classification alone, matching
    # gate_admission_decision()'s pre-existing behavior byte-for-byte --
    # requirement 3), or every AI value supplied AGREED with the checker
    # (requirement 2) -- both cases resolve to the SAME signal (the
    # checker's own value, which is identical to the AI's when they
    # agreed), so the resolved statement_type/relation is always the
    # checker's.
    admission_debug: dict = {
        "work_state": VerificationState.VERIFIED,
        "relation": checker_relation,
        "intended_relation": intended_relation,
        "statement_type": checker_statement_type,
    }
    decision, admission_debug = _admission_from_resolved_signals(
        work_state=VerificationState.VERIFIED,
        relation=checker_relation,
        intended_relation=intended_relation,
        statement_type=checker_statement_type,
        debug=admission_debug,
    )
    reasons.append(admission_debug.get("reason", decision))

    return {
        "decision": decision,
        "reasons": reasons,
        "ai_proposed": ai_proposed,
        "deterministic_checked": deterministic_checked,
        "agreement": True if ai_supplied else None,
    }
