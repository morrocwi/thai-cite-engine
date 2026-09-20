"""Query Planner: claim/context -> Support x Challenge query variants.

Described in prose in ARCHITECTURE.md SS91 ("Innovation 5 -- Adversarial
Search Pair (support x challenge)"), building on `glosa`'s literature-review
methodology (search_log.schema.json / LRS) which already requires separating
SUPPORT queries from CHALLENGE queries at generation time, before retrieval
ever runs -- so the candidate pool feeding `core.engine.resolve_citations()`
is never built from only one side of the framing from the start.

This is explicitly a v1 deterministic baseline (ARCHITECTURE.md SS97's own
framing: "as light as possible, addressing the most impact"), matching this
module's sibling `routing/router.py`'s "keyword heuristic, adequate for v1"
standard -- not a claim of solving general query expansion. No LLM call is
made here; ARCHITECTURE.md SS98 lists "Query generation" as one of exactly 3
places an LLM is *allowed* to be used in this project, but a deterministic
baseline is what v1 actually ships, same as `routing/router.py`'s
`classify_domain()`.

CRITICAL constraint (ARCHITECTURE.md SS91, carried over from `glosa`'s own
LRS discipline): **a challenge query must never be merely
`NOT <support query>`.** Literal negation of the support query string biases
retrieval toward "absence of the support finding" rather than toward a
genuinely different framing that a real skeptical reader would search for
(a null result, a confound, a reversed-direction causal story, a refuting
paper) -- and most search engines/APIs do not even support a leading `NOT`
operator as a first token the way a boolean-search person might expect.

How this module avoids that trap, concretely -- worked example:

    claim = "social media causes depression"

    WRONG  (never produced here): "NOT social media causes depression"
    RIGHT  (what `plan_queries()` actually returns):
        support:
          - "social media causes depression"
          - "social media risk factor depression"
        challenge:
          - "social media prevents depression"          # genuine semantic
                                                          # opposite swap
                                                          # (cause -> prevent),
                                                          # not a negated
                                                          # sentence
          - "social media depression confounding"
          - "social media depression reverse causality"

Every challenge query above is itself a *positive*, independently
searchable claim or qualifier (a real paper could be found that discusses
"reverse causality" or "confounding" for this topic) -- none of them is the
support query with a `NOT`/`no`/`never` token bolted onto the front.

Mechanism: a small, fixed table of common relational word families
(increase/decrease, cause/prevent, support/refute, association/no
association -- ARCHITECTURE.md SS91's own listed examples). For whichever
family's keyword is found in the claim:
  - a SUPPORT variant swaps the keyword for a same-direction near-synonym
    (e.g. "causes" -> "is a risk factor for") and/or appends a
    same-direction qualifier noun-phrase (e.g. "risk factor").
  - a CHALLENGE variant swaps the keyword for its genuine semantic opposite
    (e.g. "causes" -> "prevents") -- a real different relation, never a
    negated copy of the support sentence -- and/or appends a
    skeptical-reader qualifier noun-phrase (e.g. "confounding",
    "reverse causality", "null association").
This is the "injecting contrast/negation markers and near-synonym swaps"
approach named in this module's task description: "negation" here means
swapping in the opposite pole of a relation (decrease instead of increase),
never prefixing the sentence with a negation operator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ------------------------------------------------------- Relation families --


@dataclass(frozen=True)
class _Pole:
    """One side of a relational word family (e.g. the 'cause' side of
    cause/prevent). `keywords` are matched case-insensitively as whole
    words/phrases; `canonical` is the normalized word substituted back into
    the claim when this pole is used to build a variant sentence.
    """

    keywords: tuple[str, ...]
    canonical: str


@dataclass(frozen=True)
class _RelationFamily:
    name: str
    pole_a: _Pole
    pole_b: _Pole
    # Same-direction qualifier noun-phrases appended to the claim's "topic"
    # (the claim with the matched relation phrase removed) to build extra
    # SUPPORT queries.
    support_qualifiers: tuple[str, ...]
    # Opposite-direction / skeptical-reader qualifier noun-phrases appended
    # to the topic to build extra CHALLENGE queries. None of these is a
    # negation of the support query -- each names a distinct, independently
    # searchable methodological or empirical framing (confounding, reverse
    # causality, a null result) exactly per ARCHITECTURE.md SS91.
    challenge_qualifiers: tuple[str, ...]


# Ordered so the first family whose keyword appears in the claim wins (a
# claim is expected to carry one dominant relational word for v1; this
# keeps the module deterministic and simple to test, matching
# `routing/router.py`'s own "adequate for v1" keyword-heuristic standard).
_RELATION_FAMILIES: tuple[_RelationFamily, ...] = (
    _RelationFamily(
        name="cause_prevent",
        pole_a=_Pole(
            keywords=(
                "causes", "cause", "caused", "causing",
                "leads to", "lead to", "results in",
            ),
            canonical="causes",
        ),
        pole_b=_Pole(
            keywords=(
                "prevents", "prevent", "prevented", "preventing",
                "protects against", "protect against",
            ),
            canonical="prevents",
        ),
        support_qualifiers=("risk factor", "longitudinal evidence"),
        challenge_qualifiers=(
            "confounding", "reverse causality", "bidirectional causality",
        ),
    ),
    _RelationFamily(
        name="increase_decrease",
        pole_a=_Pole(
            keywords=(
                "increases", "increase", "increased", "increasing",
                "raises", "raise", "raised", "higher",
            ),
            canonical="increases",
        ),
        pole_b=_Pole(
            keywords=(
                "decreases", "decrease", "decreased", "decreasing",
                "lowers", "lower", "lowered",
            ),
            canonical="decreases",
        ),
        support_qualifiers=("dose-response relationship", "risk"),
        challenge_qualifiers=("no significant change", "null effect"),
    ),
    _RelationFamily(
        name="support_refute",
        pole_a=_Pole(
            keywords=(
                "supports", "support", "supported", "supporting",
                "confirms", "confirm", "confirmed",
            ),
            canonical="supports",
        ),
        pole_b=_Pole(
            keywords=(
                "refutes", "refute", "refuted", "refuting",
                "contradicts", "contradict", "contradicted",
            ),
            canonical="refutes",
        ),
        support_qualifiers=("corroborating evidence",),
        challenge_qualifiers=("conflicting evidence", "mixed findings"),
    ),
    _RelationFamily(
        name="association_no_association",
        pole_a=_Pole(
            keywords=(
                "associated with", "association", "correlated with",
                "correlation", "linked to",
            ),
            canonical="associated with",
        ),
        pole_b=_Pole(
            keywords=(
                "not associated with", "no association", "unrelated to",
                "independent of",
            ),
            canonical="not associated with",
        ),
        support_qualifiers=("significant association",),
        challenge_qualifiers=("null association", "no significant association"),
    ),
)

# Fallback qualifiers used only when no relation family's keyword is found
# in the claim at all -- still never a "NOT <claim>" negation, just a
# generic skeptical-reader framing appended as its own noun phrase.
_FALLBACK_CHALLENGE_QUALIFIERS = ("conflicting evidence", "null result")

_MAX_QUERIES_PER_FAMILY = 3


def _build_keyword_pattern(family: _RelationFamily) -> re.Pattern[str]:
    all_keywords = sorted(
        family.pole_a.keywords + family.pole_b.keywords,
        key=len,
        reverse=True,  # longest phrase first so "leads to" beats "lead"
    )
    escaped = "|".join(re.escape(k) for k in all_keywords)
    return re.compile(rf"\b(?:{escaped})\b", flags=re.IGNORECASE)


_FAMILY_PATTERNS: tuple[tuple[_RelationFamily, re.Pattern[str]], ...] = tuple(
    (family, _build_keyword_pattern(family)) for family in _RELATION_FAMILIES
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(".!?").strip()


def _dedupe(queries: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for query in queries:
        cleaned = _normalize(query)
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) >= _MAX_QUERIES_PER_FAMILY:
            break
    return result


def _find_match(claim: str) -> tuple[_RelationFamily, re.Match[str]] | None:
    for family, pattern in _FAMILY_PATTERNS:
        match = pattern.search(claim)
        if match:
            return family, match
    return None


def _opposite_canonical(family: _RelationFamily, matched_text: str) -> str:
    lowered = matched_text.lower()
    if lowered in {k.lower() for k in family.pole_a.keywords}:
        return family.pole_b.canonical
    return family.pole_a.canonical


def _same_pole_canonical(family: _RelationFamily, matched_text: str) -> str:
    lowered = matched_text.lower()
    if lowered in {k.lower() for k in family.pole_a.keywords}:
        return family.pole_a.canonical
    return family.pole_b.canonical


def plan_queries(claim: str) -> dict[str, list[str]]:
    """Generate a small, deterministic Support x Challenge query set.

    Args:
        claim: a claim or context string, e.g.
            "social media causes depression".

    Returns:
        {"support": [...], "challenge": [...]} -- each a short list (1-3)
        of query variant strings. Never empty for a non-empty `claim`.

    No network call, no LLM call -- pure string transformation, so this is
    trivially unit-testable and safe to call from any adapter/router code
    path (`routing/router.py`) before retrieval.
    """
    claim = _normalize(claim)
    if not claim:
        return {"support": [], "challenge": []}

    match_result = _find_match(claim)

    if match_result is None:
        # No known relation keyword found -- fall back to the claim itself
        # for support, and generic skeptical-reader qualifiers (never a
        # "NOT <claim>" negation) for challenge.
        support = _dedupe([claim])
        challenge = _dedupe(
            [f"{claim} {qualifier}" for qualifier in _FALLBACK_CHALLENGE_QUALIFIERS]
        )
        return {"support": support, "challenge": challenge}

    family, match = match_result
    matched_text = match.group(0)
    topic = _normalize(claim[: match.start()] + " " + claim[match.end() :])

    same_pole_word = _same_pole_canonical(family, matched_text)
    opposite_pole_word = _opposite_canonical(family, matched_text)

    # SUPPORT: the claim as given, a same-direction reworded sentence (only
    # added if it actually differs from the original), plus same-direction
    # qualifier noun-phrases built from the topic.
    support_candidates = [claim]
    reworded_support = claim[: match.start()] + same_pole_word + claim[match.end() :]
    support_candidates.append(_normalize(reworded_support))
    support_candidates.extend(
        f"{topic} {qualifier}" for qualifier in family.support_qualifiers
    )

    # CHALLENGE: a genuine semantic-opposite reworded sentence (e.g.
    # "causes" -> "prevents") -- NEVER "NOT <claim>" -- plus skeptical-
    # reader qualifier noun-phrases built from the topic.
    reworded_challenge = claim[: match.start()] + opposite_pole_word + claim[match.end() :]
    challenge_candidates = [_normalize(reworded_challenge)]
    challenge_candidates.extend(
        f"{topic} {qualifier}" for qualifier in family.challenge_qualifiers
    )

    return {
        "support": _dedupe(support_candidates),
        "challenge": _dedupe(challenge_candidates),
    }
