"""Regression test for the round-5-review-found, same-day-fixed bug:
`evidence/relation.py`'s open direction-word classes had base forms
("decrease"/"increase") but not their common regular inflections
("decreased"/"decreasing"), so a claim/evidence pair whose contradiction
was expressed only via a past-tense or -ing form was misread as POSITIVE
polarity and reached a false SUPPORTS -> false ADMIT, live-reproduced in
the pure-deterministic path (no AI participation at all -- see
docs/KNOWN_ISSUES.md's "Round 5's own adversarial review" section for the
full writeup). Fixed via `_en_inflection_bases()`, a narrow regular-
inflection stripper (no irregular verbs, no doubling/y->i rules).
"""

from __future__ import annotations

from thaicite.evidence.relation import classify_relation
from thaicite.evidence.verifier import check_claim_evidence
from thaicite.core.models import Decision


def test_past_tense_inflection_still_detected_as_reversal():
    # The exact claim/passage pair the round-5 final review used to find
    # this bug live.
    relation = classify_relation(
        "Results showed that the new fertilizer decreased rice yield in "
        "Isan by 12 percent compared to control plots.",
        "The new fertilizer increases rice yield in Isan.",
    )
    assert relation == "CHALLENGES", (
        "past-tense 'decreased' must be recognized as the negative-class "
        "counterpart of the claim's 'increases', not silently treated as "
        "no signal at all (was SUPPORTS before the inflection fix)"
    )


def test_ing_form_inflection_still_detected_as_reversal():
    relation = classify_relation(
        "Results showed the intervention was decreasing patient recovery "
        "time significantly.",
        "The intervention increases patient recovery time.",
    )
    assert relation == "CHALLENGES"


def test_pure_deterministic_path_no_longer_false_admits_on_inflection_gap():
    # The full end-to-end regression: check_claim_evidence() with NO
    # ai_relation/ai_statement_type at all (exactly what verify_cite()/
    # find_cites()/the CLI use today) must not ADMIT a citation whose
    # evidence directly contradicts the claim, purely because the
    # contradiction was expressed via an inflected form of an already-
    # registered direction word.
    result = check_claim_evidence(
        claim="The new fertilizer increases rice yield in Isan.",
        passage=(
            "Results showed that the new fertilizer decreased rice yield "
            "in Isan by 12 percent compared to control plots."
        ),
    )
    assert result["decision"] != Decision.ADMIT, (
        f"false ADMIT reproduced: {result}"
    )


def test_base_form_case_still_works_no_regression():
    # Sanity: the original (already-passing) base-form case must still work
    # after adding inflection handling -- this is not a replacement for the
    # exact-form matching, only an addition.
    relation = classify_relation(
        "Results showed that social media increases depression among "
        "adolescents.",
        "social media increases depression among adolescents",
    )
    assert relation == "SUPPORTS"


def test_irregular_verb_form_honestly_still_not_covered():
    # Documented limitation, not a bug: _en_inflection_bases() only handles
    # REGULAR English inflections (-ed/-ing/-es/-s). An irregular past
    # tense ("fell", not "falled") is not derivable from "fall" by suffix
    # stripping and is expected to still be missed. This test exists so a
    # future fix that closes this gap is a deliberate change, not a silent
    # one -- if this starts failing, update this test and the docs
    # together, don't just delete it.
    relation = classify_relation(
        "Results showed that the intervention fell sharply after week two.",
        "The intervention rises steadily over time.",
    )
    assert relation != "CHALLENGES", (
        "if this now passes, the irregular-verb gap has been closed -- "
        "update docs/KNOWN_ISSUES.md's inflection-fix note accordingly "
        "instead of just deleting this assertion"
    )
