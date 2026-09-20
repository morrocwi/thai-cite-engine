"""Claim <-> evidence relation labeling (ARCHITECTURE.md SS72-73, SS90, SS98).

`classify_relation()` is a DETERMINISTIC v1 BASELINE heuristic, not a claim
of solving general natural-language entailment. It looks for explicit
negation/contrast markers occurring near shared topic terms to flag
CHALLENGES vs. SUPPORTS, falls back to CONTEXT_ONLY when only loose topical
overlap exists with no clear directional signal, and returns UNCLEAR when
the evidence text is too thin (empty, or too short) to say anything at all.

Role separation (ARCHITECTURE.md SS98 -- API / LLM / deterministic Gate):
this function may be swapped for a stronger classifier later (an LLM call,
for instance) via the SAME signature (`(passage, claim) -> relation label`),
but a relation label must NEVER itself set the final ADMIT/REJECT/HOLD
decision -- that stays entirely inside the deterministic gate in
`evidence/verifier.py::gate_admission_decision`, which only ever *reads*
the relation label as one input among several. This module does not import
from, and is not imported by, the decision logic in a way that would let it
influence `work.state` or any gate boolean.
"""

from __future__ import annotations

import re
from typing import Literal

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

# Explicit negation/contrast markers -- single tokens and short phrases.
# Deliberately conservative: only clear directional-reversal language, not
# every hedge word (hedges without negation fall through to CONTEXT_ONLY /
# SUPPORTS on shared-term strength instead).
_NEGATION_TOKENS = {
    "not", "no", "none", "never", "without", "cannot", "fails", "failed",
    "disprove", "disproves", "disproved", "refute", "refutes", "refuted",
    "contradict", "contradicts", "contradicted", "contrary", "null",
    "lacks", "lacking", "absent", "insufficient", "unsupported",
}
_NEGATION_PHRASES = (
    "no evidence", "no significant", "no association", "null association",
    "did not", "does not", "do not", "in contrast", "on the contrary",
    "as opposed to", "rather than", "however", "despite", "whereas",
    "failed to", "fails to", "not associated", "not support",
    "does not support", "did not support",
)

# Below this many tokens the passage is treated as too thin to classify at
# all, regardless of what it contains -- UNCLEAR, not a guess.
_MIN_PASSAGE_TOKENS = 4
# Fewer than this many shared, non-stopword topic tokens is "loose overlap"
# (CONTEXT_ONLY), not a clear directional signal (SUPPORTS/CHALLENGES).
_MIN_SHARED_FOR_DIRECTIONAL = 2
# Word-distance window (in tokens) within which a negation marker must
# appear, relative to a shared topic term, to count as "near" it.
_PROXIMITY_WINDOW = 8


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9฀-๿]+", (text or "").lower())


def _keywords(tokens: list[str]) -> set[str]:
    return {t for t in tokens if len(t) > 2 and t not in _STOPWORDS}


def _negation_positions(tokens: list[str]) -> list[int]:
    """Token indices where a negation marker (single-token or phrase) starts."""
    positions: list[int] = []
    joined = " " + " ".join(tokens) + " "
    for i, tok in enumerate(tokens):
        if tok in _NEGATION_TOKENS:
            positions.append(i)
    for phrase in _NEGATION_PHRASES:
        phrase_tokens = phrase.split()
        n = len(phrase_tokens)
        for i in range(len(tokens) - n + 1):
            if tokens[i : i + n] == phrase_tokens:
                positions.append(i)
    return positions


def _shared_term_positions(tokens: list[str], shared: set[str]) -> list[int]:
    return [i for i, tok in enumerate(tokens) if tok in shared]


def _negation_near_shared_terms(
    tokens: list[str], shared: set[str], window: int = _PROXIMITY_WINDOW
) -> bool:
    neg_positions = _negation_positions(tokens)
    if not neg_positions:
        return False
    shared_positions = _shared_term_positions(tokens, shared)
    if not shared_positions:
        return False
    for neg_i in neg_positions:
        for shared_i in shared_positions:
            if abs(neg_i - shared_i) <= window:
                return True
    return False


def classify_relation(passage: str, claim: str) -> RelationValue:
    """Deterministic v1 baseline: passage vs. claim -> relation label.

    NOT a general entailment classifier -- a caller with a stronger model
    (e.g. an LLM-backed classifier, per ARCHITECTURE.md SS98's "LLM is used
    in exactly 3 places" -- claim<->evidence relation is one of them) may
    substitute it via the same `(passage, claim) -> RelationValue`
    signature. The label this returns feeds
    `evidence/verifier.py::gate_admission_decision` as one input; it never
    sets `decision` itself.

    Returns:
        "UNCLEAR"      -- passage or claim missing/too thin to say anything.
        "CHALLENGES"   -- shared topic terms found near an explicit
                           negation/contrast marker.
        "SUPPORTS"     -- a meaningful number of shared topic terms found,
                           with no negation/contrast marker nearby.
        "CONTEXT_ONLY" -- only loose topical overlap (few/no shared terms,
                           or shared terms too sparse to call directional),
                           with no negation signal either.
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

    if _negation_near_shared_terms(passage_tokens, shared):
        return "CHALLENGES"

    if len(shared) >= _MIN_SHARED_FOR_DIRECTIONAL:
        return "SUPPORTS"

    return "CONTEXT_ONLY"
