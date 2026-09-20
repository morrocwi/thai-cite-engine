"""Claim <-> evidence relation labeling (ARCHITECTURE.md SS72-73, SS90, SS98).

`classify_relation()` is a DETERMINISTIC v1 BASELINE "consistency checker",
not a claim of solving general natural-language entailment/NLI. It is
explicitly a two-stage design:

  1. `_interpret_relation()` -- the heuristic reader. It looks for explicit
     negation/contrast markers (English and Thai) AND directional-reversal
     antonym pairs (e.g. claim says "increase", passage says "decrease")
     occurring near shared topic terms, to flag CHALLENGES vs. SUPPORTS. It
     falls back to CONTEXT_ONLY when only loose topical overlap exists with
     no clear directional signal, and returns UNCLEAR when the evidence
     text is too thin (empty, or too short) to say anything at all.
  2. `_consistency_check()` -- a SEPARATE, SMALLER, independent re-check of
     the same passage/claim pair, using only the highest-confidence
     negation markers and directional-reversal pairs (a tighter,
     harder-to-fool list than stage 1's). Its only question is "does a
     reversal signal exist here or not", answered from its own tight list,
     independently of whatever stage 1 concluded.

`classify_relation()` runs both stages and compares them. If stage 1 says
SUPPORTS but stage 2's tight, independent check finds a reversal signal
stage 1 missed (or stage 1 says CHALLENGES but stage 2's tight check finds
no reversal at all, i.e. stage 1's call rests on a weaker signal stage 2
does not trust) -- that disagreement is treated as INFORMATION, not an
error to silently resolve by picking one side, so the combined result is
"UNCLEAR" rather than a possibly-wrong SUPPORTS. This is the deliberate
guard against the most dangerous failure mode this whole project exists to
prevent: a false SUPPORTS silently producing a false `ADMIT` downstream.

Role separation (ARCHITECTURE.md SS98 -- API / LLM / deterministic Gate):
`_interpret_relation()`'s exact signature (`(passage, claim) -> relation
label`) may be swapped for a stronger classifier later (an LLM call, for
instance), with `_consistency_check()` staying in place afterwards
regardless, as an independent sanity layer over whatever interpreter is in
use. But a relation label must NEVER itself set the final
ADMIT/REJECT/HOLD decision -- that stays entirely inside the deterministic
gate in `evidence/verifier.py::gate_admission_decision`, which only ever
*reads* the relation label as one input among several. This module does
not import from, and is not imported by, the decision logic in a way that
would let it influence `work.state` or any gate boolean.
"""

from __future__ import annotations

from typing import Literal

from thaicite.normalize.tokenize import tokenize as _shared_tokenize

RelationValue = Literal["SUPPORTS", "CHALLENGES", "CONTEXT_ONLY", "UNCLEAR"]

# Same low-information-word philosophy as evidence/verifier.py's _STOPWORDS
# (kept as a separate, smaller list here deliberately -- this module must
# stay independently testable/swappable without coupling to the gate
# module's internals; see the module docstring's role-separation note).
_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "in", "on", "for", "to", "with",
    "is", "are", "was", "were", "be", "been", "being",
    "this", "that", "these", "those", "it", "its",
    "study", "analysis", "review", "paper", "work", "works",
    "from", "has", "have", "had", "not", "no",
}

# ---------------------------------------------------------------------------
# Explicit negation/contrast markers -- single tokens and short phrases,
# English AND Thai (post-tokenization; the shared tokenizer in
# normalize/tokenize.py real-segments Thai, so a marker like "ไม่พบว่า" comes
# back as separate tokens ["ไม่", "พบ", "ว่า"] -- see _NEGATION_PHRASES for
# the multi-token Thai forms, and note that the single token "ไม่" ("not")
# alone already covers every ไม่-prefixed form pythainlp emits as
# ["ไม่", <rest>] -- ไม่ได้, ไม่มี, ไม่เป็น, ไม่พบ, ไม่ปรากฏ, etc.).
#
# Deliberately conservative in what counts as a STAGE-1 (interpreter)
# marker: only clear directional-reversal/negation language, not every
# hedge word (hedges without negation fall through to CONTEXT_ONLY /
# SUPPORTS on shared-term strength instead).
# ---------------------------------------------------------------------------
_NEGATION_TOKENS = {
    # English
    "not", "no", "none", "never", "without", "cannot", "fails", "failed",
    "disprove", "disproves", "disproved", "refute", "refutes", "refuted",
    "contradict", "contradicts", "contradicted", "contrary", "null",
    "lacks", "lacking", "absent", "insufficient", "unsupported",
    # Thai -- "ไม่" ("not") is the single-token prefix pythainlp splits off
    # every ไม่-form onto (ไม่ได้, ไม่มี, ไม่เป็น, ไม่พบ, ไม่ปรากฏ, ...); "มิได้"
    # ("did not" / "was not") tokenizes as one whole token on its own.
    "ไม่", "มิได้",
}
_NEGATION_PHRASES = (
    "no evidence", "no significant", "no association", "null association",
    "did not", "does not", "do not", "in contrast", "on the contrary",
    "as opposed to", "rather than", "however", "despite", "whereas",
    "failed to", "fails to", "not associated", "not support",
    "does not support", "did not support",
    # Thai -- space-joined because _NEGATION_PHRASES is matched against the
    # ALREADY-TOKENIZED stream (see _negation_positions), and pythainlp
    # segments "ไม่พบว่า" into exactly these three tokens in order.
    "ไม่ พบ ว่า",
)

# ---------------------------------------------------------------------------
# Directional-reversal antonym pairs. Each side is a tuple of one or more
# tokens (post-tokenization), because some Thai forms segment into more
# than one token (e.g. "สูงขึ้น" -> ["สูง", "ขึ้น"], "แย่ลง" -> ["แย่", "ลง"]).
#
# When the CLAIM uses one member of a pair and the PASSAGE uses the OTHER
# member near a shared topic term, that is exactly as strong a
# CHALLENGES signal as an explicit negation marker -- so it feeds the SAME
# "negation/contrast near shared terms -> CHALLENGES" code path as the
# negation-marker logic (see _reversal_positions / _reversal_near_shared),
# not a separate bolted-on branch.
# ---------------------------------------------------------------------------
_DIRECTIONAL_PAIRS: frozenset[tuple[tuple[str, ...], tuple[str, ...]]] = frozenset(
    {
        (("increase",), ("decrease",)),
        (("increases",), ("decreases",)),
        (("raise",), ("lower",)),
        (("raises",), ("lowers",)),
        (("rise",), ("fall",)),
        (("rises",), ("falls",)),
        (("cause",), ("prevent",)),
        (("causes",), ("prevents",)),
        (("positive",), ("negative",)),
        (("improve",), ("worsen",)),
        (("improves",), ("worsens",)),
        (("higher",), ("lower",)),
        (("more",), ("less",)),
        (("gain",), ("loss",)),
        (("benefit",), ("harm",)),
        (("เพิ่ม",), ("ลด",)),
        (("เพิ่มขึ้น",), ("ลดลง",)),
        (("สูง", "ขึ้น"), ("ต่ำ", "ลง")),
        (("ดีขึ้น",), ("แย่", "ลง")),
        (("มี",), ("ไม่", "มี")),
        (("พบ",), ("ไม่", "พบ")),
    }
)

# STAGE 2 (`_consistency_check`) uses a strict subset of the negation/
# directional-pair vocabulary above -- only the markers/pairs that are
# domain-specific enough to be hard to fool by coincidental use elsewhere
# in a sentence. Deliberately excludes single common words that are
# directional pairs in THIS domain but ordinary English/Thai words
# everywhere else (e.g. "more"/"less", "positive"/"negative",
# "higher"/"lower", "gain"/"loss", "benefit"/"harm") and excludes the
# softer contrast phrases ("however", "despite", "whereas", "in contrast",
# "on the contrary", "as opposed to", "rather than", "contrary", "null",
# "lacks", "lacking", "absent", "insufficient", "unsupported") that stage 1
# treats as signal but that are individually easy to trigger by accident.
_TIGHT_NEGATION_TOKENS = {
    "not", "no", "none", "never", "without", "cannot",
    "ไม่", "มิได้",
}
_TIGHT_NEGATION_PHRASES = (
    "no evidence", "no significant", "no association", "null association",
    "did not", "does not", "do not",
    "not associated", "not support", "does not support", "did not support",
    "failed to", "fails to",
    "ไม่ พบ ว่า",
)
_TIGHT_DIRECTIONAL_PAIRS: frozenset[tuple[tuple[str, ...], tuple[str, ...]]] = frozenset(
    {
        (("increase",), ("decrease",)),
        (("increases",), ("decreases",)),
        (("raise",), ("lower",)),
        (("raises",), ("lowers",)),
        (("rise",), ("fall",)),
        (("rises",), ("falls",)),
        (("cause",), ("prevent",)),
        (("causes",), ("prevents",)),
        (("improve",), ("worsen",)),
        (("improves",), ("worsens",)),
        (("เพิ่ม",), ("ลด",)),
        (("เพิ่มขึ้น",), ("ลดลง",)),
        (("สูง", "ขึ้น"), ("ต่ำ", "ลง")),
        (("ดีขึ้น",), ("แย่", "ลง")),
        (("มี",), ("ไม่", "มี")),
        (("พบ",), ("ไม่", "พบ")),
    }
)

# Below this many tokens the passage is treated as too thin to classify at
# all, regardless of what it contains -- UNCLEAR, not a guess.
_MIN_PASSAGE_TOKENS = 4
# Fewer than this many shared, non-stopword topic tokens is "loose overlap"
# (CONTEXT_ONLY), not a clear directional signal (SUPPORTS/CHALLENGES).
_MIN_SHARED_FOR_DIRECTIONAL = 2
# Word-distance window (in tokens) within which a negation marker or
# directional-reversal counterpart must appear, relative to a shared topic
# term, to count as "near" it.
_PROXIMITY_WINDOW = 8


def _tokenize(text: str) -> list[str]:
    # Delegates to the shared Thai-aware tokenizer (normalize/tokenize.py)
    # -- see its module docstring for why the bare regex this used to be
    # collapses an unspaced Thai sentence into a single token.
    return _shared_tokenize(text)


def _keywords(tokens: list[str]) -> set[str]:
    return {t for t in tokens if len(t) > 2 and t not in _STOPWORDS}


def _find_subsequence_positions(tokens: list[str], seq: tuple[str, ...]) -> list[int]:
    """Start indices where `seq` occurs as a contiguous run in `tokens`."""
    n = len(seq)
    if n == 0 or n > len(tokens):
        return []
    return [i for i in range(len(tokens) - n + 1) if tuple(tokens[i : i + n]) == seq]


def _negation_positions(
    tokens: list[str],
    negation_tokens: set[str] = _NEGATION_TOKENS,
    negation_phrases: tuple[str, ...] = _NEGATION_PHRASES,
) -> list[int]:
    """Token indices where a negation marker (single-token or phrase) starts."""
    positions: list[int] = []
    for i, tok in enumerate(tokens):
        if tok in negation_tokens:
            positions.append(i)
    for phrase in negation_phrases:
        positions.extend(_find_subsequence_positions(tokens, tuple(phrase.split())))
    return positions


def _reversal_positions(
    passage_tokens: list[str],
    claim_tokens: list[str],
    pairs: frozenset[tuple[tuple[str, ...], tuple[str, ...]]] = _DIRECTIONAL_PAIRS,
) -> list[int]:
    """Passage token positions where the counterpart of a directional word
    actually used in the CLAIM appears in the PASSAGE (e.g. claim says
    "increase", passage says "decrease", or vice versa, for any pair in
    `pairs`). These positions are directional-reversal signals in exactly
    the same sense as an explicit negation marker.
    """
    positions: list[int] = []
    for side_a, side_b in pairs:
        if _find_subsequence_positions(claim_tokens, side_a):
            positions.extend(_find_subsequence_positions(passage_tokens, side_b))
        if _find_subsequence_positions(claim_tokens, side_b):
            positions.extend(_find_subsequence_positions(passage_tokens, side_a))
    return positions


def _shared_term_positions(tokens: list[str], shared: set[str]) -> list[int]:
    return [i for i, tok in enumerate(tokens) if tok in shared]


def _reversal_near_shared_terms(
    passage_tokens: list[str],
    claim_tokens: list[str],
    shared: set[str],
    negation_tokens: set[str],
    negation_phrases: tuple[str, ...],
    directional_pairs: frozenset[tuple[tuple[str, ...], tuple[str, ...]]],
    window: int = _PROXIMITY_WINDOW,
) -> bool:
    """True iff an explicit negation marker OR a directional-reversal
    counterpart of a claim word appears in the passage within `window`
    tokens of a shared topic term. Both signal kinds share this one
    proximity check -- a directional-reversal pair is not a separate
    bolted-on branch, it feeds the same negation-near-shared-terms logic.
    """
    reversal_positions = _negation_positions(passage_tokens, negation_tokens, negation_phrases)
    reversal_positions += _reversal_positions(passage_tokens, claim_tokens, directional_pairs)
    if not reversal_positions:
        return False
    shared_positions = _shared_term_positions(passage_tokens, shared)
    if not shared_positions:
        return False
    return any(abs(r - s) <= window for r in reversal_positions for s in shared_positions)


def _interpret_relation(passage: str, claim: str) -> RelationValue:
    """STAGE 1 -- the heuristic reader (see module docstring). Deterministic
    v1 baseline: passage vs. claim -> relation label, using the full
    negation-marker + directional-reversal-pair vocabulary.

    Returns:
        "UNCLEAR"      -- passage or claim missing/too thin to say anything.
        "CHALLENGES"   -- shared topic terms found near an explicit
                           negation/contrast marker, or near a
                           directional-reversal counterpart of a word the
                           claim uses.
        "SUPPORTS"     -- a meaningful number of shared topic terms found,
                           with no negation/contrast/reversal signal nearby.
        "CONTEXT_ONLY" -- only loose topical overlap (few/no shared terms,
                           or shared terms too sparse to call directional),
                           with no negation/reversal signal either.
    """
    passage_tokens = _tokenize(passage)
    claim_tokens = _tokenize(claim)

    if not claim_tokens or len(passage_tokens) < _MIN_PASSAGE_TOKENS:
        return "UNCLEAR"

    passage_kw = _keywords(passage_tokens)
    claim_kw = _keywords(claim_tokens)
    shared = passage_kw & claim_kw

    if not shared:
        # No topical connection to the claim at all -- not enough signal
        # to say anything about direction, and not even loose overlap.
        return "UNCLEAR"

    if _reversal_near_shared_terms(
        passage_tokens, claim_tokens, shared,
        _NEGATION_TOKENS, _NEGATION_PHRASES, _DIRECTIONAL_PAIRS,
    ):
        return "CHALLENGES"

    if len(shared) >= _MIN_SHARED_FOR_DIRECTIONAL:
        return "SUPPORTS"

    return "CONTEXT_ONLY"


def _consistency_check(passage: str, claim: str) -> bool:
    """STAGE 2 -- an independent, deliberately SMALLER re-check that asks
    only "is there an obvious reversal signal here?", using ONLY the
    highest-confidence negation markers and directional-reversal pairs (see
    `_TIGHT_NEGATION_TOKENS` / `_TIGHT_NEGATION_PHRASES` /
    `_TIGHT_DIRECTIONAL_PAIRS`). It does not itself compute SUPPORTS or
    CONTEXT_ONLY -- it returns a plain bool, "reversal signal present near
    a shared topic term, per the tight list". `classify_relation()` uses
    disagreement between this and `_interpret_relation()` as a trigger for
    UNCLEAR rather than trusting either stage alone.
    """
    passage_tokens = _tokenize(passage)
    claim_tokens = _tokenize(claim)

    if not claim_tokens or len(passage_tokens) < _MIN_PASSAGE_TOKENS:
        return False

    passage_kw = _keywords(passage_tokens)
    claim_kw = _keywords(claim_tokens)
    shared = passage_kw & claim_kw
    if not shared:
        return False

    return _reversal_near_shared_terms(
        passage_tokens, claim_tokens, shared,
        _TIGHT_NEGATION_TOKENS, _TIGHT_NEGATION_PHRASES, _TIGHT_DIRECTIONAL_PAIRS,
    )


def classify_relation(passage: str, claim: str) -> RelationValue:
    """Deterministic v1 baseline CONSISTENCY CHECKER: passage vs. claim ->
    relation label. See the module docstring for the two-stage design.

    NOT a general entailment classifier -- `_interpret_relation()` (stage 1)
    may be substituted with a stronger model (e.g. an LLM-backed classifier,
    per ARCHITECTURE.md SS98's "LLM is used in exactly 3 places" -- claim
    <-> evidence relation is one of them) later via the same
    `(passage, claim) -> RelationValue` signature, with `_consistency_check`
    (stage 2) staying in place afterwards as an independent sanity layer
    regardless of what stage 1 becomes.

    The label this returns feeds
    `evidence/verifier.py::gate_admission_decision` as one input; it never
    sets `decision` itself.

    Returns:
        "UNCLEAR"      -- passage or claim missing/too thin to say
                           anything, OR the two stages disagree about
                           whether a reversal signal is present (see
                           module docstring -- disagreement is information,
                           not silently resolved by picking one side).
        "CHALLENGES"   -- both stages agree a reversal signal is present.
        "SUPPORTS"     -- stage 1 found a meaningful number of shared
                           topic terms with no reversal signal, and stage 2
                           independently confirms no reversal signal either.
        "CONTEXT_ONLY" -- only loose topical overlap, no reversal signal
                           from either stage.
    """
    interpreted = _interpret_relation(passage, claim)
    if interpreted == "UNCLEAR":
        return interpreted

    stage2_found_reversal = _consistency_check(passage, claim)
    stage1_called_reversal = interpreted == "CHALLENGES"

    if stage1_called_reversal != stage2_found_reversal:
        return "UNCLEAR"

    return interpreted
