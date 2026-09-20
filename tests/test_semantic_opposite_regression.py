"""Regression tests for a new adversarial category: SEMANTIC_OPPOSITE_WITHOUT_NEGATION.

`classify_relation()` (evidence/relation.py) must correctly flag a claim and
a passage that assert OPPOSITE directional facts even when the passage uses
no explicit negation word at all -- the directional-reversal antonym-pair
machinery (`_DIRECTIONAL_PAIRS` / `_reversal_positions`) exists precisely for
this case (e.g. claim says "increase", passage says "decrease": no "not",
"no", "never" anywhere, but the two statements are still in direct semantic
conflict).

This file proves, offline, with the real unmocked `classify_relation()`:
  - increase/decrease, raise/lower, cause/prevent English antonym pairs
    reliably return CHALLENGES (never SUPPORTS).
  - the exact Thai example from the task -- "กฎหมายอิสลามเพิ่มสิทธิของผู้หญิง"
    (claim: Islamic law increases women's rights) vs. "ผลการศึกษาไม่พบว่า
    กฎหมายอิสลามเพิ่มสิทธิของผู้หญิง" (evidence: the study found no [support
    for the claim that] Islamic law increases women's rights) -- must NOT
    return SUPPORTS.

No mocking of `classify_relation()`/`_interpret_relation()`/
`_consistency_check()` -- these are the real, production functions, matching
the offline-only discipline already used by test_cite_use_gate.py.
"""

from __future__ import annotations

import pytest

from thaicite.evidence.relation import classify_relation

# ---------------------------------------------------------------------------
# English directional-reversal antonym pairs -- no negation word present,
# only opposite directional vocabulary near shared topic terms.
# ---------------------------------------------------------------------------

_ENGLISH_OPPOSITE_CASES = [
    pytest.param(
        "This program will increase student retention rates across the "
        "district.",
        "A five-year cohort study of the program found the program will "
        "decrease student retention rates across the district in practice.",
        id="increase_vs_decrease",
    ),
    pytest.param(
        "The policy proposal aims to raise minimum wage levels for "
        "hospitality workers.",
        "Economic analysis of the policy found the policy will lower "
        "minimum wage levels for hospitality workers in practice.",
        id="raise_vs_lower",
    ),
    pytest.param(
        "Proponents argue the intervention will cause hospital "
        "readmission among discharged patients.",
        "The randomized trial found the intervention will prevent "
        "hospital readmission among discharged patients.",
        id="cause_vs_prevent",
    ),
]


@pytest.mark.parametrize("claim, passage", _ENGLISH_OPPOSITE_CASES)
def test_semantic_opposite_without_negation_never_supports(claim: str, passage: str) -> None:
    """No explicit negation marker ("not"/"no"/"never"/...) appears in
    either string in any of these pairs -- only an antonym near the shared
    topic vocabulary. `classify_relation()` must never call this SUPPORTS."""
    result = classify_relation(passage, claim)
    assert result != "SUPPORTS", (result, passage, claim)


@pytest.mark.parametrize("claim, passage", _ENGLISH_OPPOSITE_CASES)
def test_semantic_opposite_without_negation_flags_challenges(claim: str, passage: str) -> None:
    """Stronger assertion: for these clean, unambiguous antonym-pair cases
    (both stages of the two-stage design should agree), the correct label is
    CHALLENGES specifically, not merely "not SUPPORTS"."""
    result = classify_relation(passage, claim)
    assert result == "CHALLENGES", (result, passage, claim)


def test_semantic_opposite_neither_string_contains_a_negation_marker() -> None:
    """Sanity check on the fixtures themselves: confirms these cases really
    do test the ANTONYM-PAIR path, not the negation-marker path -- neither
    claim nor passage contains an obvious negation word."""
    negation_markers = ("not", "no ", "never", "n't", "without", "cannot")
    for claim, passage in [(p.values[0], p.values[1]) for p in _ENGLISH_OPPOSITE_CASES]:
        lowered_claim = claim.lower()
        lowered_passage = passage.lower()
        for marker in negation_markers:
            assert marker not in lowered_claim, (marker, claim)
        # The passage legitimately doesn't need to be negation-free for
        # this sanity check -- only that a CHALLENGES verdict on it is
        # attributable to the antonym pair, which the assertions above
        # already exercise directly via classify_relation() itself. Confirm
        # explicitly for the passages too, since none of these fixtures
        # were written to need a negation word at all.
        for marker in negation_markers:
            assert marker not in lowered_passage, (marker, passage)


# ---------------------------------------------------------------------------
# The exact Thai example given in the task.
# ---------------------------------------------------------------------------

THAI_CLAIM = "กฎหมายอิสลามเพิ่มสิทธิของผู้หญิง"  # Islamic law increases women's rights
THAI_EVIDENCE = (
    "ผลการศึกษาไม่พบว่ากฎหมายอิสลามเพิ่มสิทธิของผู้หญิง"
)  # the study found no [support for the claim that] ... increases ...


def test_thai_islamic_law_womens_rights_pair_does_not_support() -> None:
    """The task's own exact worked example -- this pair must NOT return
    SUPPORTS. The Thai evidence string embeds an explicit "ไม่พบว่า" (did not
    find that) negation phrase directly in front of the claim's own
    "กฎหมายอิสลามเพิ่มสิทธิของผู้หญิง" wording, so this exercises the
    negation-marker path (not only the antonym-pair path) on real,
    unspaced Thai text through the shared pythainlp-backed tokenizer."""
    result = classify_relation(THAI_EVIDENCE, THAI_CLAIM)
    assert result != "SUPPORTS", result


def test_thai_islamic_law_womens_rights_pair_flags_challenges_or_unclear() -> None:
    """Stronger check: the negation phrase "ไม่พบว่า" immediately precedes
    the shared claim vocabulary, well within the proximity window, so this
    should resolve to CHALLENGES specifically (both stages should agree a
    reversal signal is present). Accept UNCLEAR too (the fail-closed
    fallback when the two independent stages disagree) as still a passing,
    safe outcome -- the one and only forbidden outcome is SUPPORTS."""
    result = classify_relation(THAI_EVIDENCE, THAI_CLAIM)
    assert result in ("CHALLENGES", "UNCLEAR"), result


def test_thai_negation_marker_ไม่_is_present_in_evidence_tokens() -> None:
    """Sanity check on the fixture: confirms the Thai evidence string really
    does carry the "ไม่" negation token once tokenized (via the shared
    Thai-aware tokenizer), so the assertions above are attributable to a
    real negation signal, not an accidental artifact."""
    from thaicite.normalize.tokenize import tokenize

    tokens = tokenize(THAI_EVIDENCE)
    assert "ไม่" in tokens, tokens
