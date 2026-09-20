"""Unit tests for the Query Planner (routing/query_planner.py, ARCHITECTURE.md SS91).

Offline, no network, no LLM -- pure string transformation.
"""

from __future__ import annotations

from thaicite.routing.query_planner import plan_queries


def test_returns_support_and_challenge_keys():
    result = plan_queries("social media causes depression")
    assert set(result.keys()) == {"support", "challenge"}
    assert result["support"]
    assert result["challenge"]


def test_challenge_is_never_literal_not_support():
    result = plan_queries("social media causes depression")
    for support_query in result["support"]:
        for challenge_query in result["challenge"]:
            assert challenge_query != f"NOT {support_query}"
            assert not challenge_query.lower().startswith("not ")
            assert not challenge_query.lower().startswith("no ")


def test_cause_prevent_family_swaps_to_genuine_opposite():
    result = plan_queries("social media causes depression")
    assert any("prevents" in q for q in result["challenge"])
    assert any("confounding" in q for q in result["challenge"])
    assert any("reverse causality" in q for q in result["challenge"])
    # original claim preserved as a support query
    assert "social media causes depression" in result["support"]


def test_increase_decrease_family():
    result = plan_queries("screen time increases anxiety in teenagers")
    assert any("decreases" in q for q in result["challenge"])
    assert any("null effect" in q for q in result["challenge"])


def test_support_refute_family():
    result = plan_queries("the trial supports the vaccine's efficacy")
    assert any("refutes" in q for q in result["challenge"])


def test_association_family():
    result = plan_queries("air pollution is associated with asthma")
    assert any("not associated with" in q for q in result["challenge"])
    assert any("null association" in q for q in result["challenge"])


def test_no_known_relation_keyword_falls_back_without_negation():
    result = plan_queries("graph neural networks for traffic forecasting")
    assert result["support"] == ["graph neural networks for traffic forecasting"]
    assert result["challenge"]
    for q in result["challenge"]:
        assert not q.lower().startswith("not ")


def test_empty_claim_returns_empty_lists():
    assert plan_queries("") == {"support": [], "challenge": []}
    assert plan_queries("   ") == {"support": [], "challenge": []}


def test_deterministic_repeat_calls_identical():
    a = plan_queries("smoking causes lung cancer")
    b = plan_queries("smoking causes lung cancer")
    assert a == b


def test_no_duplicate_queries_within_a_list():
    result = plan_queries("stress causes heart disease")
    assert len(result["support"]) == len(set(q.lower() for q in result["support"]))
    assert len(result["challenge"]) == len(set(q.lower() for q in result["challenge"]))
