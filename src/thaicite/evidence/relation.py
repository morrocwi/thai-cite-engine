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

---------------------------------------------------------------------------
ROUND 4 RESTRUCTURE (2026-09-20, founder-approved) -- primary signal is now
a proposition-level polarity read, not raw keyword overlap
---------------------------------------------------------------------------

Round 3 (above) already demoted raw keyword-overlap-implies-SUPPORTS behind
a negation/directional-reversal check, but that check itself still lived as
a bolted-on correction INSIDE `_interpret_relation()`, and the directional-
reversal signal was still keyed off a CLOSED antonym-PAIR list
(`_DIRECTIONAL_PAIRS`). Confirmed, live-reproduced gaps in that pair list
("reduces" vs "increases", "protects" vs "damages", "promotes" vs
"suppresses", "slows" vs "accelerates", "expands" vs "restricts" -- none of
these pairs was in the table) kept producing false SUPPORTS, and enumerating
more individual pairs does not close this class of gap in general (the
founder's own point: this is not a "add pair #51" problem).

This round's structural fix, in two parts:

  1. `extract_proposition()` -- a small, honestly-heuristic (NOT a real
     semantic parser) reading of (passage, claim) into subject / predicate
     polarity (POSITIVE=matches / NEGATIVE=reverses / NEUTRAL=absent) /
     shared-term set. Its polarity read is no longer keyed to a closed
     PAIR list alone -- it also consults two open WORD-CLASS sets,
     `_POSITIVE_DIRECTION_WORDS` / `_NEGATIVE_DIRECTION_WORDS`: any claim
     word in one class vs. any passage word in the OTHER class, near the
     shared terms, is a reversal signal -- no pair-by-pair enumeration
     required, so a new word only ever needs to be added to ONE class to
     be correctly opposed to EVERY existing word already in the other
     class (this is the architectural change; the closed-pair mechanism
     from round 3 still exists underneath it as one contributing input,
     unchanged, not removed).
  2. `classify_relation()` now consults `extract_proposition()`'s polarity
     as the FIRST-CLASS/PRIMARY signal (Task 3). The pre-existing two-stage
     `_interpret_relation()`/`_consistency_check()` design (`_checker_relation()`
     below) is kept in full, demoted to a SECONDARY CHECKER role: it still
     independently re-derives a relation label from its own (narrower,
     pair-list-keyed) view, and `classify_relation()` still falls back to
     UNCLEAR rather than trusting a primary-signal SUPPORTS the checker
     disagrees with -- the same "disagreement is information, not silently
     resolved" discipline round 3 established, just re-anchored around the
     new primary signal instead of purely between the two old stages.

Honesty about what this does and does not close (see also
`extract_proposition()`'s own docstring): this is still a deterministic,
closed-vocabulary heuristic, not an NLI/entailment model -- a genuinely
novel antonym pair using neither an existing word class nor a recognizable
negation marker (e.g. two completely unrelated domain-specific verbs the
class lists have no member for at all) still will not be caught. What
changed is the SHAPE of the vocabulary problem: from "list every pair" (a
combinatorial, never-finished task) to "classify each word's direction
once" (a linear, still-finite-but-much-smaller task) -- the confirmed round
3 gaps (reduces/increases, protects/damages, promotes/suppresses,
slows/accelerates, expands/restricts) are closed by adding those verbs to
the two word-class sets, not by adding five new pair entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from thaicite.normalize.tokenize import tokenize as _shared_tokenize

RelationValue = Literal["SUPPORTS", "CHALLENGES", "CONTEXT_ONLY", "UNCLEAR", "QUALIFIES"]

# TASK 1 -- predicate-polarity of a passage relative to a claim's own
# directional/relational word: does the passage MATCH the claim's
# direction, REVERSE it, or say nothing recognizable about it at all.
PredicatePolarity = Literal["POSITIVE", "NEGATIVE", "NEUTRAL"]

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

# ---------------------------------------------------------------------------
# ROUND 4 -- open WORD-CLASS direction vocabulary (see module docstring's
# "ROUND 4 RESTRUCTURE" section). Each word belongs to exactly one of these
# two sets; ANY member of `_POSITIVE_DIRECTION_WORDS` used in the CLAIM is
# treated as reversed by ANY member of `_NEGATIVE_DIRECTION_WORDS` used in
# the PASSAGE (and vice versa) near the shared topic terms -- this is what
# closes a pair like "reduces"/"increases" or "protects"/"damages" without
# either exact pair ever being enumerated: "reduces" only had to be added to
# the negative class to be correctly opposed to "increases", "increase",
# "raises", "improves", ... -- every existing positive-class word at once.
#
# This is deliberately a SUPERSET of `_DIRECTIONAL_PAIRS`'s own vocabulary
# (both mechanisms run; see `_reversal_near_shared_terms`), not a
# replacement for it -- round 3's closed-pair check still runs unchanged.
# ---------------------------------------------------------------------------
#
# Deliberately EXCLUDES the Thai "มี"/"พบ" ("has"/"found[-that]") presence-
# pair -- unlike every other member here, "พบ" in particular is also the
# ordinary Thai verb for "found" used in report-framing ("ผลการศึกษาพบว่า"
# -- "the study found that"), not a direction word at all in that use, and
# putting it in an open class would wrongly oppose it against ANY
# negative-class claim word (e.g. "ลด" -- "reduce") whenever a passage
# merely reports a finding. "มี"/"ไม่มี" and "พบ"/"ไม่พบ" stay as an
# explicit, narrowly-scoped PAIR in `_DIRECTIONAL_PAIRS`/
# `_TIGHT_DIRECTIONAL_PAIRS` only (each opposes its own exact negated
# form, never the whole open class).
_POSITIVE_DIRECTION_WORDS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("increase",), ("increases",), ("raise",), ("raises",),
        ("rise",), ("rises",), ("cause",), ("causes",),
        ("positive",), ("improve",), ("improves",), ("higher",),
        ("more",), ("gain",), ("gains",), ("benefit",), ("benefits",),
        ("promote",), ("promotes",), ("protect",), ("protects",),
        ("expand",), ("expands",), ("accelerate",), ("accelerates",),
        ("เพิ่ม",), ("เพิ่มขึ้น",), ("สูง", "ขึ้น"), ("ดีขึ้น",),
    }
)
_NEGATIVE_DIRECTION_WORDS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("decrease",), ("decreases",), ("lower",), ("lowers",),
        ("fall",), ("falls",), ("prevent",), ("prevents",),
        ("negative",), ("worsen",), ("worsens",), ("less",),
        ("loss",), ("losses",), ("harm",), ("harms",),
        ("suppress",), ("suppresses",), ("damage",), ("damages",),
        ("restrict",), ("restricts",), ("slow",), ("slows",),
        ("reduce",), ("reduces",),
        ("ลด",), ("ลดลง",), ("ต่ำ", "ลง"), ("แย่", "ลง"),
    }
)

# Tight (checker-stage) variant, same philosophy as `_TIGHT_DIRECTIONAL_PAIRS`
# above -- drops the single generic words that are directional in this
# domain but ordinary words everywhere else ("more"/"less"/"positive"/
# "negative"/"higher"/"gain(s)"/"benefit(s)"/"loss(es)"/"harm(s)"), keeps
# the specific, hard-to-trigger-by-accident verbs, INCLUDING the five
# confirmed round-3 gap words -- this is why the checker stage can now
# independently confirm those five pairs too, not only the primary signal.
_TIGHT_POSITIVE_DIRECTION_WORDS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("increase",), ("increases",), ("raise",), ("raises",),
        ("rise",), ("rises",), ("cause",), ("causes",),
        ("improve",), ("improves",),
        ("promote",), ("promotes",), ("protect",), ("protects",),
        ("expand",), ("expands",), ("accelerate",), ("accelerates",),
        ("เพิ่ม",), ("เพิ่มขึ้น",), ("สูง", "ขึ้น"), ("ดีขึ้น",),
    }
)
_TIGHT_NEGATIVE_DIRECTION_WORDS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("decrease",), ("decreases",), ("lower",), ("lowers",),
        ("fall",), ("falls",), ("prevent",), ("prevents",),
        ("worsen",), ("worsens",),
        ("suppress",), ("suppresses",), ("damage",), ("damages",),
        ("restrict",), ("restricts",), ("slow",), ("slows",),
        ("reduce",), ("reduces",),
        ("ลด",), ("ลดลง",), ("ต่ำ", "ลง"), ("แย่", "ลง"),
    }
)

# ---------------------------------------------------------------------------
# TASK 2 -- QUALIFIES scope/condition/population cue phrases (English +
# Thai). Matched the same way `_NEGATION_PHRASES` is: against the
# ALREADY-TOKENIZED stream, space-joined so pythainlp's own token split of
# each Thai phrase is what actually gets matched (see the comment on
# `_NEGATION_PHRASES` above for why).
# ---------------------------------------------------------------------------
_SCOPE_QUALIFIER_PHRASES = (
    "only among",
    "specifically for",
    "in the subgroup of",
    "limited to",
    # Thai -- "เฉพาะใน" -> ["เฉพาะ", "ใน"], "เฉพาะกลุ่ม" -> ["เฉพาะ", "กลุ่ม"],
    # "จำกัดเฉพาะ" -> ["จำกัด", "เฉพาะ"] under the shared pythainlp tokenizer.
    "เฉพาะ ใน",
    "เฉพาะ กลุ่ม",
    "จำกัด เฉพาะ",
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


def _direction_class_present(tokens: list[str], word_class: frozenset[tuple[str, ...]]) -> bool:
    return any(_find_subsequence_positions(tokens, seq) for seq in word_class)


def _direction_class_positions(
    tokens: list[str], word_class: frozenset[tuple[str, ...]]
) -> list[int]:
    positions: list[int] = []
    for seq in word_class:
        positions.extend(_find_subsequence_positions(tokens, seq))
    return positions


def _class_reversal_positions(
    passage_tokens: list[str],
    claim_tokens: list[str],
    positive_words: frozenset[tuple[str, ...]],
    negative_words: frozenset[tuple[str, ...]],
) -> list[int]:
    """ROUND 4 -- open word-CLASS counterpart of `_reversal_positions()`.
    Instead of requiring the claim's word and the passage's word to be an
    explicitly-enumerated PAIR, this only requires each to be a member of
    the OPPOSITE class -- so "reduces" (negative class) opposes "increases"
    (positive class) the moment both are in their respective classes, with
    no `("increases",), ("reduces",)` pair entry ever needed.
    """
    positions: list[int] = []
    if _direction_class_present(claim_tokens, positive_words):
        positions.extend(_direction_class_positions(passage_tokens, negative_words))
    if _direction_class_present(claim_tokens, negative_words):
        positions.extend(_direction_class_positions(passage_tokens, positive_words))
    return positions


def _same_class_positions(
    passage_tokens: list[str],
    claim_tokens: list[str],
    positive_words: frozenset[tuple[str, ...]],
    negative_words: frozenset[tuple[str, ...]],
) -> list[int]:
    """Positions in the passage where a word from the SAME direction class
    as the claim's own directional word appears -- the "matches" half of
    `extract_proposition()`'s POSITIVE/NEGATIVE/NEUTRAL polarity read.
    """
    positions: list[int] = []
    if _direction_class_present(claim_tokens, positive_words):
        positions.extend(_direction_class_positions(passage_tokens, positive_words))
    if _direction_class_present(claim_tokens, negative_words):
        positions.extend(_direction_class_positions(passage_tokens, negative_words))
    return positions


def _scope_qualifier_positions(tokens: list[str]) -> list[int]:
    positions: list[int] = []
    for phrase in _SCOPE_QUALIFIER_PHRASES:
        positions.extend(_find_subsequence_positions(tokens, tuple(phrase.split())))
    return positions


def _scope_qualifier_near_shared(
    passage_tokens: list[str], shared: set[str], window: int = _PROXIMITY_WINDOW
) -> bool:
    """TASK 2 -- True iff a scope/condition/population qualifier cue phrase
    (e.g. "only among", "limited to", "เฉพาะใน") sits within `window` tokens
    of a shared topic term in the passage. Used by `classify_relation()` to
    flag QUALIFIES: the evidence affirms the claim's own direction, but only
    under a narrower scope/condition than the claim itself states.
    """
    cue_positions = _scope_qualifier_positions(passage_tokens)
    if not cue_positions:
        return False
    shared_positions = _shared_term_positions(passage_tokens, shared)
    if not shared_positions:
        return False
    return any(abs(c - s) <= window for c in cue_positions for s in shared_positions)


def _reversal_near_shared_terms(
    passage_tokens: list[str],
    claim_tokens: list[str],
    shared: set[str],
    negation_tokens: set[str],
    negation_phrases: tuple[str, ...],
    directional_pairs: frozenset[tuple[tuple[str, ...], tuple[str, ...]]],
    positive_words: frozenset[tuple[str, ...]] | None = None,
    negative_words: frozenset[tuple[str, ...]] | None = None,
    window: int = _PROXIMITY_WINDOW,
) -> bool:
    """True iff an explicit negation marker, a directional-reversal
    counterpart of a claim word (closed-pair list, round 3), OR an
    opposite-direction-CLASS word (open class list, round 4 -- only
    consulted when `positive_words`/`negative_words` are supplied) appears
    in the passage within `window` tokens of a shared topic term. All three
    signal kinds share this one proximity check -- neither the pair list
    nor the class list is a separate bolted-on branch, both feed the same
    negation-near-shared-terms logic.
    """
    reversal_positions = _negation_positions(passage_tokens, negation_tokens, negation_phrases)
    reversal_positions += _reversal_positions(passage_tokens, claim_tokens, directional_pairs)
    if positive_words is not None and negative_words is not None:
        reversal_positions += _class_reversal_positions(
            passage_tokens, claim_tokens, positive_words, negative_words
        )
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
        _POSITIVE_DIRECTION_WORDS, _NEGATIVE_DIRECTION_WORDS,
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
        _TIGHT_POSITIVE_DIRECTION_WORDS, _TIGHT_NEGATIVE_DIRECTION_WORDS,
    )


def _checker_relation(passage: str, claim: str) -> RelationValue:
    """SECONDARY CHECKER (round 4 rename of what used to be
    `classify_relation()`'s entire body, pre-restructure) -- the original
    two-stage `_interpret_relation()`/`_consistency_check()` design, kept
    in place UNCHANGED in mechanism (see module docstring's "ROUND 4
    RESTRUCTURE" section), only re-scoped from primary decision-maker to
    an independent sanity check that `classify_relation()` consults after
    its own primary, proposition-level polarity read.

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


def _heuristic_subject(claim_tokens: list[str], max_tokens: int = 4) -> str | None:
    """Best-effort "topic noun-phrase-ish span" for `EvidenceProposition
    .subject` -- explicitly NOT a real constituency/dependency parse (see
    `EvidenceProposition`'s own docstring). Takes the claim's own leading
    non-stopword keyword tokens, in order, up to `max_tokens`. A reasonable
    v1 for this domain: a claim sentence like "social media causes
    depression among adolescents" or "AI tutoring improves critical
    thinking in students" leads with its own topic, so the first few
    content words are a workable proxy for "what this claim is about"
    without attempting any real syntax. Returns `None` when the claim has
    no recognizable content word at all.
    """
    kw = [t for t in claim_tokens if len(t) > 2 and t not in _STOPWORDS]
    if not kw:
        return None
    return " ".join(kw[:max_tokens])


def _predicate_polarity(
    passage_tokens: list[str], claim_tokens: list[str], shared: set[str]
) -> PredicatePolarity:
    """TASK 1's polarity read: does the passage, near the shared topic
    terms, MATCH the claim's own directional/relational word (POSITIVE),
    REVERSE it via negation or an antonym/opposite-class word (NEGATIVE),
    or say nothing recognizable either way (NEUTRAL)? Reuses the SAME
    negation/directional-pair/direction-class machinery
    `_interpret_relation()` (the checker) uses -- this is "exposed as a
    reusable signal", per Task 1, not a second, independently-invented
    detector.
    """
    if not shared:
        return "NEUTRAL"

    if _reversal_near_shared_terms(
        passage_tokens, claim_tokens, shared,
        _NEGATION_TOKENS, _NEGATION_PHRASES, _DIRECTIONAL_PAIRS,
        _POSITIVE_DIRECTION_WORDS, _NEGATIVE_DIRECTION_WORDS,
    ):
        return "NEGATIVE"

    shared_positions = _shared_term_positions(passage_tokens, shared)
    if shared_positions:
        match_positions = _same_class_positions(
            passage_tokens, claim_tokens, _POSITIVE_DIRECTION_WORDS, _NEGATIVE_DIRECTION_WORDS
        )
        for side_a, side_b in _DIRECTIONAL_PAIRS:
            if _find_subsequence_positions(claim_tokens, side_a):
                match_positions += _find_subsequence_positions(passage_tokens, side_a)
            if _find_subsequence_positions(claim_tokens, side_b):
                match_positions += _find_subsequence_positions(passage_tokens, side_b)
        if match_positions and any(
            abs(p - s) <= _PROXIMITY_WINDOW for p in match_positions for s in shared_positions
        ):
            return "POSITIVE"

    if len(shared) >= _MIN_SHARED_FOR_DIRECTIONAL:
        # No directional word/class `extract_proposition` recognizes was
        # echoed nearby (or the claim uses no recognized directional word
        # at all) -- fall back to the same plain
        # topical-overlap-implies-support floor `_interpret_relation()`
        # itself falls back to, so this signal stays consistent with the
        # checker for claims outside the direction vocabulary entirely.
        return "POSITIVE"

    return "NEUTRAL"


@dataclass(frozen=True)
class EvidenceProposition:
    """TASK 1 -- lightweight, DETERMINISTIC v1 heuristic reading of a
    (passage, claim) pair. Explicitly NOT a full semantic-role/subject-
    verb-object parser -- same honesty standard as every other v1 baseline
    in this repo (see module docstring). A reasonable v1, per the task:
    locate the claim's own key directional/relational word (or
    antonym-pair/direction-class member) in the passage if present, and
    record whether its polarity there matches, reverses, or is absent
    relative to the claim -- nothing more structured than that is
    attempted here.

    subject
        Best-effort topic noun-phrase-ish span, heuristic (see
        `_heuristic_subject`) -- not a real parse, and may be `None` for a
        claim with no recognizable content word.
    predicate_polarity
        "POSITIVE" (the passage echoes the claim's own directional word or
        direction-class member, unreversed, near the shared topic terms --
        or the claim carries no recognized directional word at all and
        there is still meaningful topical overlap), "NEGATIVE" (an
        explicit negation marker or an antonym/opposite-class word appears
        near the shared topic terms), or "NEUTRAL" (neither -- including
        too little shared vocabulary to say anything).
    shared_terms_with_claim
        The keyword-overlap set `classify_relation()` already computed
        for its own purposes -- kept here too, but demoted, per Task 3, to
        ONE input signal among several, never the primary driver of a
        relation decision by itself.
    """

    subject: str | None
    predicate_polarity: PredicatePolarity
    shared_terms_with_claim: frozenset[str]


def extract_proposition(passage: str, claim: str) -> EvidenceProposition:
    """TASK 1 -- passage vs. claim -> `EvidenceProposition`. See
    `EvidenceProposition`'s own docstring for exactly what each field does
    and does not claim to be. Returns `predicate_polarity="NEUTRAL"` and an
    empty `shared_terms_with_claim` when the claim or passage is missing or
    too thin to say anything at all (same `_MIN_PASSAGE_TOKENS` floor used
    throughout this module) -- not a guess in either direction.
    """
    passage_tokens = _tokenize(passage)
    claim_tokens = _tokenize(claim)
    subject = _heuristic_subject(claim_tokens)

    if not claim_tokens or len(passage_tokens) < _MIN_PASSAGE_TOKENS:
        return EvidenceProposition(
            subject=subject, predicate_polarity="NEUTRAL", shared_terms_with_claim=frozenset()
        )

    passage_kw = _keywords(passage_tokens)
    claim_kw = _keywords(claim_tokens)
    shared = passage_kw & claim_kw

    polarity = _predicate_polarity(passage_tokens, claim_tokens, shared)
    return EvidenceProposition(
        subject=subject, predicate_polarity=polarity, shared_terms_with_claim=frozenset(shared)
    )


def classify_relation(passage: str, claim: str) -> RelationValue:
    """Deterministic v1 baseline CONSISTENCY CHECKER: passage vs. claim ->
    relation label. See the module docstring's "ROUND 4 RESTRUCTURE"
    section for the current primary-signal-plus-checker design (Task 3).

    NOT a general entailment classifier -- the primary signal
    (`extract_proposition()`) may be substituted with a stronger model
    (e.g. an LLM-backed classifier, per ARCHITECTURE.md SS98's "LLM is
    used in exactly 3 places" -- claim <-> evidence relation is one of
    them) later, with `_checker_relation()` staying in place afterwards as
    an independent sanity layer regardless of what the primary signal
    becomes.

    The label this returns feeds
    `evidence/verifier.py::gate_admission_decision` as one input; it never
    sets `decision` itself.

    Returns:
        "UNCLEAR"      -- passage or claim missing/too thin to say
                           anything at all, OR the primary signal calls
                           SUPPORTS while the secondary checker
                           independently flags a reversal it did not
                           (disagreement is information, not silently
                           resolved by picking one side -- same discipline
                           round 3 established).
        "CHALLENGES"   -- the primary signal (`extract_proposition()`'s
                           predicate_polarity) found a reversal -- trusted
                           directly, even when the secondary checker's own
                           narrower (pair-list-keyed) view does not
                           independently confirm it (this is precisely
                           what closes a pair like "reduces"/"increases"
                           that is outside the checker's closed list; see
                           module docstring for what this does and does
                           not close).
        "SUPPORTS"     -- the primary signal found the passage echoing the
                           claim's own direction (or found meaningful
                           topical overlap with no directional word in the
                           claim at all) with no reversal, and the
                           secondary checker does not independently flag
                           one either.
        "CONTEXT_ONLY" -- only loose topical overlap, no reversal signal
                           from the primary signal.
        "QUALIFIES"    -- the primary signal did not find a reversal, but a
                           scope/condition/population qualifier cue (e.g.
                           "only among", "limited to", "เฉพาะใน") sits near
                           the shared topic terms -- the evidence affirms
                           the claim's own direction, but under a narrower
                           scope than the claim itself states (Task 2).
    """
    passage_tokens = _tokenize(passage)
    claim_tokens = _tokenize(claim)

    if not claim_tokens or len(passage_tokens) < _MIN_PASSAGE_TOKENS:
        return "UNCLEAR"

    passage_kw = _keywords(passage_tokens)
    claim_kw = _keywords(claim_tokens)
    shared = passage_kw & claim_kw
    if not shared:
        return "UNCLEAR"

    polarity = _predicate_polarity(passage_tokens, claim_tokens, shared)

    # TASK 2 -- QUALIFIES: the primary signal did not find a reversal, but
    # a scope-narrowing cue sits right next to the shared topic terms.
    if polarity != "NEGATIVE" and _scope_qualifier_near_shared(passage_tokens, shared):
        return "QUALIFIES"

    if polarity == "NEGATIVE":
        # PRIMARY signal (Task 3): trust a reversal it finds directly, even
        # when the secondary checker's own narrower vocabulary does not
        # independently confirm it.
        return "CHALLENGES"

    checker = _checker_relation(passage, claim)

    if polarity == "POSITIVE":
        if len(shared) < _MIN_SHARED_FOR_DIRECTIONAL:
            return "CONTEXT_ONLY"
        if checker == "CHALLENGES":
            # The secondary checker independently flagged a reversal the
            # primary signal's own detection missed -- do not silently
            # trust a possibly-wrong SUPPORTS (same "disagreement is
            # information" discipline as round 3).
            return "UNCLEAR"
        return "SUPPORTS"

    # polarity == "NEUTRAL" -- the claim carries no directional word/class
    # `extract_proposition` recognizes at all, or too little shared
    # vocabulary to say anything; defer entirely to the secondary checker
    # (unchanged behavior from before this restructure for this case).
    return checker
