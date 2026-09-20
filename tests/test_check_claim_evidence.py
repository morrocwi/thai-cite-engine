"""Unit tests for `evidence/verifier.py::check_claim_evidence()` -- the
AI-Reader-vs-deterministic-Checker consistency gate (2026-09-20 ThaiCite
role-change fix). No network call, no LLM call -- `ai_statement_type`/
`ai_relation` are plain string parameters supplied directly by the test,
simulating what a calling AI agent's Scout+Reader role would propose.
"""

from __future__ import annotations

from thaicite.core.models import Decision, RelationLabel
from thaicite.evidence.verifier import check_claim_evidence


# ---------------------------------------------------------------------------
# Worked example (a): AI and checker AGREE -> ADMIT.
# ---------------------------------------------------------------------------


def test_agree_result_supports_admits():
    claim = "AI tutoring increases student time on task"
    passage = "The study found that AI tutoring significantly increased time on task among students."

    result = check_claim_evidence(
        claim=claim,
        passage=passage,
        ai_statement_type="RESULT",
        ai_relation="SUPPORTS",
    )

    assert result["deterministic_checked"]["statement_type"] == "RESULT"
    assert result["deterministic_checked"]["relation"] == "SUPPORTS"
    assert result["agreement"] is True
    assert result["ai_proposed"] == {"statement_type": "RESULT", "relation": "SUPPORTS"}
    assert result["decision"] == Decision.ADMIT
    assert any("agrees" in r for r in result["reasons"])


# ---------------------------------------------------------------------------
# Worked example (b): AI says SUPPORTS, checker says CHALLENGES -> HOLD,
# both views recorded.
# ---------------------------------------------------------------------------


def test_disagree_ai_supports_checker_challenges_holds_with_both_views():
    claim = "social media use causes depression"
    passage = "The trial found no significant association between social media use and depression."

    result = check_claim_evidence(
        claim=claim,
        passage=passage,
        ai_statement_type="RESULT",
        ai_relation="SUPPORTS",
    )

    assert result["deterministic_checked"]["relation"] == "CHALLENGES"
    assert result["agreement"] is False
    assert result["decision"] == Decision.HOLD
    reason_text = " | ".join(result["reasons"])
    assert "ai_relation=SUPPORTS" in reason_text
    assert "checker_relation=CHALLENGES" in reason_text
    assert "disagreement" in reason_text


# ---------------------------------------------------------------------------
# No AI input supplied -- pure deterministic fallback, matching
# gate_admission_decision()'s existing behavior.
# ---------------------------------------------------------------------------


def test_no_ai_input_falls_back_to_checker_only_agreement_none():
    claim = "AI tutoring increases student time on task"
    passage = "The study found that AI tutoring significantly increased time on task among students."

    result = check_claim_evidence(claim=claim, passage=passage)

    assert result["agreement"] is None
    assert result["ai_proposed"] is None
    assert result["decision"] == Decision.ADMIT


def test_illegal_ai_values_are_ignored_not_raised():
    claim = "AI tutoring increases student time on task"
    passage = "The study found that AI tutoring significantly increased time on task among students."

    result = check_claim_evidence(
        claim=claim,
        passage=passage,
        ai_statement_type="NOT_A_REAL_TYPE",
        ai_relation="ALSO_NOT_REAL",
    )

    # Both illegal -> treated as not supplied at all.
    assert result["agreement"] is None
    assert result["ai_proposed"] is None
    assert result["decision"] == Decision.ADMIT


def test_agreeing_statement_type_only_result_admits():
    claim = "AI tutoring increases student time on task"
    passage = "The study found that AI tutoring significantly increased time on task among students."

    result = check_claim_evidence(
        claim=claim,
        passage=passage,
        ai_statement_type="RESULT",
    )

    assert result["ai_proposed"] == {"statement_type": "RESULT", "relation": None}
    assert result["agreement"] is True
    assert result["decision"] == Decision.ADMIT


def test_disagreeing_statement_type_holds():
    claim = "AI tutoring increases student time on task"
    passage = "The study found that AI tutoring significantly increased time on task among students."

    result = check_claim_evidence(
        claim=claim,
        passage=passage,
        ai_statement_type="HYPOTHESIS",
    )

    assert result["deterministic_checked"]["statement_type"] == "RESULT"
    assert result["agreement"] is False
    assert result["decision"] == Decision.HOLD
    assert any("ai_statement_type=HYPOTHESIS" in r for r in result["reasons"])


def test_intended_relation_challenges_reject_on_mismatch():
    claim = "AI tutoring increases student time on task"
    passage = "The study found that AI tutoring significantly increased time on task among students."

    result = check_claim_evidence(
        claim=claim,
        passage=passage,
        ai_statement_type="RESULT",
        ai_relation="SUPPORTS",
        intended_relation=RelationLabel.CHALLENGES,
    )

    assert result["agreement"] is True
    assert result["decision"] == Decision.REJECT
