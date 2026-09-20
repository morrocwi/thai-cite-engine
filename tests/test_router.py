"""Unit tests for the Source Router (routing/router.py).

Offline only -- no network call, no live adapter instantiation beyond the
adapter classes' own no-argument constructors (which do not touch the
network themselves; only `.search()` does).
"""

from __future__ import annotations

from thaicite.adapters.crossref import CrossrefAdapter
from thaicite.adapters.openalex import OpenAlexAdapter
from thaicite.adapters.pubmed import PubMedAdapter
from thaicite.adapters.thaijo import ThaiJOAdapter
from thaicite.core.models import VerificationState
from thaicite.routing.router import (
    GENERAL,
    HEALTH,
    THAI,
    THAI_HEALTH,
    classify_domain,
    route,
)

_ADAPTERS = [OpenAlexAdapter(), CrossrefAdapter(), ThaiJOAdapter(), PubMedAdapter()]


# ------------------------------------------------------- domain classification --


def test_classify_general_no_thai_no_health_signal():
    assert classify_domain("", "graph neural networks for traffic forecasting") == GENERAL


def test_classify_thai_by_script():
    assert classify_domain("", "การศึกษาไทยเรื่องประวัติศาสตร์") == THAI


def test_classify_thai_by_keyword_in_english():
    assert classify_domain("", "a study of Chiang Mai tourism recovery") == THAI


def test_classify_health_english():
    assert classify_domain("", "clinical trial of a new cancer drug") == HEALTH


def test_classify_thai_health_mixed():
    assert classify_domain("", "โรคเบาหวานในประเทศไทย diabetes prevalence") == THAI_HEALTH


def test_context_and_query_both_checked():
    # Thai signal only in context, English-only query string.
    assert classify_domain("โรคเบาหวานในประเทศไทย", "diabetes prevalence study") == THAI_HEALTH


# --------------------------------------------------------- adapter ordering --


def test_general_domain_still_includes_thaijo_first():
    decision = route("", "graph neural networks for traffic forecasting", _ADAPTERS)
    assert decision.domain == GENERAL
    names = [a.name for a in decision.adapters]
    assert names[0] == "THAIJO"
    assert "THAIJO" in names  # explicitly not skipped for GENERAL


def test_thai_domain_thaijo_precedes_global_adapters():
    decision = route("", "การศึกษาไทยเรื่องประวัติศาสตร์", _ADAPTERS)
    assert decision.domain == THAI
    names = [a.name for a in decision.adapters]
    assert names.index("THAIJO") < names.index("OPENALEX")
    assert names.index("THAIJO") < names.index("CROSSREF")


def test_thai_health_domain_thaijo_first_pubmed_second():
    decision = route("", "โรคเบาหวานในประเทศไทย diabetes prevalence", _ADAPTERS)
    assert decision.domain == THAI_HEALTH
    names = [a.name for a in decision.adapters]
    assert names[0] == "THAIJO"
    assert names.index("PUBMED") < names.index("OPENALEX")


def test_health_domain_follows_architecture_table_no_thaijo():
    decision = route("", "clinical trial of a new cancer drug", _ADAPTERS)
    assert decision.domain == HEALTH
    names = [a.name for a in decision.adapters]
    assert "THAIJO" not in names
    assert names[0] == "PUBMED"


def test_router_only_selects_from_available_adapters():
    # Caller didn't configure PubMed -- router must not invent it.
    limited = [OpenAlexAdapter(), CrossrefAdapter(), ThaiJOAdapter()]
    decision = route("", "โรคเบาหวานในประเทศไทย diabetes prevalence", limited)
    names = [a.name for a in decision.adapters]
    assert "PUBMED" not in names
    assert "PUBMED" in decision.signals["requested_but_unavailable"]


# --------------------------------------------------------------- track status --


def test_track_status_not_queried_when_track_has_no_adapter():
    decision = route("", "clinical trial of a new cancer drug", _ADAPTERS)
    assert decision.track_status["local"] == "not_queried"
    assert decision.track_status["global"] == "ok"


def test_update_track_status_surfaces_degraded_global_track():
    decision = route("", "โรคเบาหวานในประเทศไทย diabetes prevalence", _ADAPTERS)
    fake_engine_result = {
        "verified": [],
        "rejected": {
            "query::x": {
                "reason": "adapter_error",
                "by_adapter": {
                    "PUBMED": {"state": VerificationState.TIMEOUT, "message": "timed out"},
                },
            }
        },
        "not_found_queries": {},
    }
    decision.update_track_status(fake_engine_result)
    assert decision.track_status["global"] == "degraded"
    # Local (ThaiJO) had no error evidence and is left as its a-priori "ok" --
    # a degraded global track must never make an unrelated healthy track
    # look degraded too.
    assert decision.track_status["local"] == "ok"


def test_update_track_status_does_not_hide_partial_failure():
    """One adapter erroring on a track must not be masked by a sibling's
    success on the SAME track -- this is the founder's explicit "do not
    let a degraded global track look identical to a healthy one" rule."""
    decision = route("", "graph neural networks for traffic forecasting", _ADAPTERS)
    fake_engine_result = {
        "verified": [],
        "rejected": {
            "CROSSREF:10.1/abc": {
                "reason": "failed gate(s): g6_identity",
                "state": VerificationState.REJECTED,
            },
            "query::y": {
                "reason": "adapter_error",
                "by_adapter": {
                    "OPENALEX": {"state": VerificationState.RATE_LIMITED, "message": "429"},
                },
            },
        },
        "not_found_queries": {},
    }
    decision.update_track_status(fake_engine_result)
    assert decision.track_status["global"] == "degraded"
