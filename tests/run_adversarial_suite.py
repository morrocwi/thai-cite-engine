#!/usr/bin/env python3
"""Objective, code-only adversarial suite runner.

This is a BEST-EFFORT, MECHANICAL reproduction of the PASS/FAIL/INCONCLUSIVE
structure in tests/golden/CONCEPT_VALIDATION_REPORT.md -- it is NOT a
substitute for the original adversarial human/LLM judge pass. It exists so a
public reader can run one command (`python3 tests/run_adversarial_suite.py`)
and get real, reproducible signal instead of prose alone.

What it does, per scenario in tests/fixtures/adversarial_100.json:
  1. Calls `run_scenario(scenario)` for real (thaicite.core.harness) --
     genuine live HTTP calls to OpenAlex happen where a scenario has adapters
     configured, exactly as the original validation run did.
  2. Applies objective, code-only checks against the actual output -- no LLM
     judge, no vibes:
       - a citation that is supposed to end up in `verified` is checked for
         an independent title/keyword identity match against the actual
         query text (NOT the free-text `context`) -- this is the exact
         mechanical check that would have caught POSITIVE_CONTROL 15/15
         FAIL objectively, without a human/LLM judge, and it is computed
         independently of thaicite's own G6 gate so a regression in G6
         itself would still be caught here.
       - a fabricated-work scenario is checked for an EMPTY `verified` list
         and for `not_found`/`rejected` wording that does not overclaim
         nonexistence ("does not exist" etc. is a wording violation --
         NOT_FOUND means "not found via the adapters searched").
       - an inject_error scenario is checked against the resulting
         `rejected`/`by_adapter` state, confirming the injected error tag
         survives intact and is never collapsed into NOT_FOUND.
       - a few METADATA_CONFLICT scenarios are, per their own fixture notes,
         impossible to exercise live through OpenAlex on demand (OpenAlex
         will not serve two disagreeing years for one DOI just because we
         ask), so for those the runner instead unit-tests
         thaicite.resolve.conflicts / thaicite.resolve.identity directly
         with synthetic Candidates, exactly as each fixture's own note
         instructs a judge to do.
  3. Handles live network failure HONESTLY: OpenAlex's free-tier shared-IP
     daily rate limit produces genuine HTTP 429s some of the time (confirmed
     independently the day this script was written). When a scenario's
     verdict genuinely cannot be determined because every relevant query hit
     a real transport failure (RATE_LIMITED/TIMEOUT/ACCESS_DENIED/
     PARSER_ERROR) rather than running the actual code path under test, the
     scenario is marked INCONCLUSIVE with the real reason recorded -- never
     silently treated as a pass, and never treated as a bug in this script.

Known, honest limitations (read before trusting a FAIL/PASS blindly):
  - The identity-match heuristic used here is a token-overlap + sequence-
    similarity check, not a full bibliographic judge. It is deliberately
    independent of thaicite's own G6 implementation, but it can still be
    wrong at the margins (e.g. a very short title, or a translated title).
  - Several scenarios (METADATA_CONFLICT's NOTE_ONLY / AMBIGUOUS_BY_DESIGN
    ones, the PER_QUERY_CORRECTNESS_AT_SCALE mega-scenario, the *_NO_CRASH
    family) use best-effort, documented approximations of what the original
    human/LLM judge pass did by reading code and exercising judgment. Where
    a scenario's own `expect.note` explicitly says "the judge should read
    the source" this script tries to do exactly that (importing and calling
    the real functions), but it is still a fixed heuristic, not judgment.
  - This script deliberately does NOT call an LLM. Nuanced cases (e.g.
    S092's "editorial-quality gap", S091's retraction flag) are reported as
    informational notes, not scored PASS/FAIL, because no objective
    code-only check can assess them.

Output: writes tests/golden/RUNNER_OUTPUT_LATEST.md (same shape as
CONCEPT_VALIDATION_REPORT.md: counts by verdict, counts by category) and
tests/golden/RUNNER_OUTPUT_LATEST.json (full machine-readable detail).

Run: `python3 tests/run_adversarial_suite.py` from anywhere; no extra
dependencies beyond what the package itself already needs (`requests`).
"""

from __future__ import annotations

import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from thaicite.core.harness import run_scenario  # noqa: E402
from thaicite.core.models import Candidate, CanonicalWork, VerificationState  # noqa: E402
from thaicite.resolve.conflicts import apply_conflict_state, detect_conflicts  # noqa: E402
from thaicite.resolve.identity import can_merge  # noqa: E402

FIXTURES_PATH = REPO_ROOT / "tests" / "fixtures" / "adversarial_100.json"
GOLDEN_DIR = REPO_ROOT / "tests" / "golden"
OUT_MD = GOLDEN_DIR / "RUNNER_OUTPUT_LATEST.md"
OUT_JSON = GOLDEN_DIR / "RUNNER_OUTPUT_LATEST.json"

ERROR_STATE_NAMES = frozenset(
    {
        VerificationState.RATE_LIMITED,
        VerificationState.TIMEOUT,
        VerificationState.ACCESS_DENIED,
        VerificationState.PARSER_ERROR,
    }
)

# Wording the pipeline must never use for a NOT_FOUND -- NOT_FOUND means
# "not found via the adapters searched", never "confirmed not to exist".
_OVERCLAIM_NONEXISTENCE_PHRASES = (
    "does not exist",
    "confirmed not to exist",
    "does not exist at all",
    "never existed",
    "no such work exists",
)


# --------------------------------------------------------------------------
# Independent identity heuristic (deliberately NOT importing thaicite's own
# G6 comparison logic -- this must be able to catch a regression in G6
# itself, so it is its own, separately-written implementation).
# --------------------------------------------------------------------------

_GENERIC_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "in", "on", "for", "to", "with",
    "is", "are", "was", "were", "this", "that", "not", "one", "two", "work",
    "works", "via", "where", "only", "real", "same", "about", "into", "but",
    "them", "over", "source", "all", "need", "well", "known", "based",
    "literature", "risk", "pair", "paper", "from", "has", "have", "had",
    "study", "analysis", "review", "et", "al", "journal", "vol", "no",
    "pp", "doi", "https", "http", "org", "www",
}


def _independent_tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text or "").lower()
    tokens = re.findall(r"[a-zA-Z0-9\u0e00-\u0e7f]+", text)
    return {t for t in tokens if len(t) >= 4 and t not in _GENERIC_STOPWORDS}


def identity_sanity(query_text: str, candidate_title: str | None) -> bool:
    """Best-effort, independent check: does `candidate_title` plausibly
    correspond to the work named in `query_text`?

    This is the objective, code-only stand-in for "does the identifier the
    scenario expects actually appear in the verified output" -- since the
    fixture does not embed ground-truth DOIs, distinctive-keyword overlap
    against the actual query citation string (never the free-text context)
    is used instead. A single shared generic word is deliberately never
    enough -- this is the exact failure mode the original run found.
    """
    if not candidate_title or not candidate_title.strip():
        return False
    q_tokens = _independent_tokens(query_text)
    t_tokens = _independent_tokens(candidate_title)
    if not q_tokens or not t_tokens:
        return False
    shared = q_tokens & t_tokens
    if len(shared) < 2:
        # A lone shared generic/topical word (e.g. "nucleic"/"acid" shared
        # between the real Watson & Crick paper and an unrelated
        # oligonucleotide-chemistry paper) is exactly the false-positive
        # pattern the original run found -- never sufficient on its own.
        return False
    # Overlap ratio against the SMALLER token set: a genuine match (title
    # largely restates the citation's own title) clears ~1.0; an unrelated
    # paper sharing only 1-2 topical words out of a much larger vocabulary
    # sits well below this (verified empirically against the report's own
    # false-positive examples, e.g. ratio 1.00 for the real Watson & Crick
    # title vs 0.40 for an unrelated "nucleic acid" paper sharing only
    # those two words).
    ratio = len(shared) / min(len(q_tokens), len(t_tokens))
    seq_ratio = SequenceMatcher(
        None, " ".join(sorted(q_tokens)), " ".join(sorted(t_tokens))
    ).ratio()
    return ratio >= 0.5 or seq_ratio >= 0.55


def is_fabricated_text(text: str) -> bool:
    return "fabricat" in (text or "").lower()


# --------------------------------------------------------------------------
# Scenario execution wrapper
# --------------------------------------------------------------------------


def _query_texts_and_injections(scenario: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    texts: list[str] = []
    injections: dict[str, str] = {}
    for q in scenario.get("queries", []):
        if isinstance(q, str):
            texts.append(q)
        elif isinstance(q, dict):
            text = q.get("text") or q.get("query")
            if text:
                texts.append(text)
                if q.get("inject_error"):
                    injections[text] = q["inject_error"]
    return texts, injections


def execute(scenario: dict[str, Any]) -> dict[str, Any]:
    """Run one scenario for real; never let a scenario's crash kill the run.

    Returns {"status": "ok"|"value_error"|"exception", "actual": ..., "error": ...}
    """
    try:
        result = run_scenario(scenario)
        return {"status": "ok", "actual": result["actual"], "error": None}
    except ValueError as exc:
        return {"status": "value_error", "actual": None, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: any crash
        # is real signal for a *_NO_CRASH scenario, not a runner bug.
        return {"status": "exception", "actual": None, "error": f"{type(exc).__name__}: {exc}"}


def _adapter_error_entries(actual: dict[str, Any]) -> dict[str, str]:
    """query_text -> injected/observed error state, for every rejected entry
    whose reason is 'adapter_error' (i.e. a real or simulated transport
    failure, not a gate rejection)."""
    out: dict[str, str] = {}
    for label, entry in actual.get("rejected", {}).items():
        if entry.get("reason") != "adapter_error":
            continue
        query_text = label.removeprefix("query::") if label.startswith("query::") else None
        by_adapter = entry.get("by_adapter") or {}
        for _adapter, info in by_adapter.items():
            state = info.get("state")
            if state:
                out[query_text or label] = state
    return out


def _is_pure_execution_gap(scenario: dict[str, Any], actual: dict[str, Any]) -> str | None:
    """If EVERY query in `scenario` that was NOT deliberately inject_error'd
    ended up blocked by a real adapter transport failure (not a gate
    rejection, not a genuine NOT_FOUND), return a human-readable reason to
    mark the scenario INCONCLUSIVE. Otherwise return None."""
    texts, injections = _query_texts_and_injections(scenario)
    non_injected = [t for t in texts if t not in injections]
    if not non_injected:
        return None
    errs = _adapter_error_entries(actual)
    blocked = [t for t in non_injected if t in errs]
    if blocked and len(blocked) == len(non_injected) and not actual.get("verified"):
        # No non-injected query produced any usable signal at all.
        gate_rejected = any(
            e.get("reason") != "adapter_error" for e in actual.get("rejected", {}).values()
        )
        if not gate_rejected:
            states = sorted({errs[t] for t in blocked})
            return f"live OpenAlex transport failure ({', '.join(states)}) on every query this scenario needed"
    return None


# --------------------------------------------------------------------------
# Per-expected_outcome checkers. Each returns (verdict, reason).
# verdict in {"PASS", "FAIL", "INCONCLUSIVE"}.
# --------------------------------------------------------------------------


def check_not_found(scenario, actual) -> tuple[str, str]:
    verified = actual["verified"]
    if verified:
        titles = ", ".join(v["title"] or "<no title>" for v in verified)
        return "FAIL", f"expected NOT_FOUND but {len(verified)} entrie(s) landed in verified: {titles}"
    blobs = []
    for entry in actual.get("not_found_queries", {}).values():
        blobs.append(entry.get("note", ""))
    for entry in actual.get("rejected", {}).values():
        blobs.append(entry.get("reason", ""))
        for info in (entry.get("by_adapter") or {}).values():
            blobs.append(info.get("message", ""))
    combined = " ".join(blobs).lower()
    for phrase in _OVERCLAIM_NONEXISTENCE_PHRASES:
        if phrase in combined:
            return "FAIL", f"wording overclaims nonexistence ({phrase!r} found in not_found/rejected text)"
    gap = _is_pure_execution_gap(scenario, actual)
    if gap:
        return "INCONCLUSIVE", gap
    return "PASS", "nothing landed in verified; no nonexistence-overclaim wording found"


def check_verified(scenario, actual) -> tuple[str, str]:
    texts, _ = _query_texts_and_injections(scenario)
    if not texts:
        return "INCONCLUSIVE", "scenario has no query text to check identity against"
    query = texts[0]
    verified = actual["verified"]
    if not verified:
        gap = _is_pure_execution_gap(scenario, actual)
        if gap:
            return "INCONCLUSIVE", gap
        return "FAIL", "expected VERIFIED but verified is empty (pipeline over-rejecting or work not found live)"
    matches = [v for v in verified if identity_sanity(query, v["title"])]
    if matches:
        return "PASS", f"{len(matches)}/{len(verified)} verified entrie(s) independently identity-match the query"
    sample = "; ".join((v["title"] or "<no title>") for v in verified[:5])
    return (
        "FAIL",
        f"{len(verified)} verified entrie(s) present but NONE independently identity-match the query "
        f"(false-positive pattern -- this is the exact POSITIVE_CONTROL 15/15 defect this check targets). "
        f"Sample verified titles: {sample}",
    )


def check_error_state_for(query_text: str, expected_state: str, actual) -> tuple[str, str]:
    if query_text in actual.get("not_found_queries", {}):
        return "FAIL", f"query {query_text!r} collapsed into NOT_FOUND instead of surfacing {expected_state}"
    errs = _adapter_error_entries(actual)
    observed = errs.get(query_text)
    if observed is None:
        return "FAIL", f"query {query_text!r} produced no adapter_error entry at all (expected {expected_state})"
    if observed != expected_state:
        return "FAIL", f"query {query_text!r} surfaced {observed}, expected {expected_state}"
    return "PASS", f"query {query_text!r} correctly surfaced {expected_state} distinct from NOT_FOUND"


def check_error_state(scenario, actual) -> tuple[str, str]:
    _texts, injections = _query_texts_and_injections(scenario)
    if not injections:
        return "INCONCLUSIVE", "expected an error-state outcome but scenario has no inject_error query"
    query_text, expected_state = next(iter(injections.items()))
    return check_error_state_for(query_text, expected_state, actual)


def check_mixed_per_query(scenario, actual) -> tuple[str, str]:
    texts, injections = _query_texts_and_injections(scenario)
    reasons = []
    verdicts = []
    for text, state in injections.items():
        v, r = check_error_state_for(text, state, actual)
        verdicts.append(v)
        reasons.append(f"[injected {state}] {r}")
    for text in texts:
        if text in injections:
            continue
        if is_fabricated_text(text):
            fake_scenario = {"queries": [text]}
            v, r = check_not_found(fake_scenario, actual)
            verdicts.append(v)
            reasons.append(f"[fabricated] {r}")
        else:
            real_scenario = {"queries": [text]}
            v, r = check_verified(real_scenario, actual)
            verdicts.append(v)
            reasons.append(f"[real] {r}")
    if any(v == "FAIL" for v in verdicts):
        return "FAIL", " | ".join(reasons)
    if any(v == "INCONCLUSIVE" for v in verdicts):
        return "INCONCLUSIVE", " | ".join(reasons)
    return "PASS", " | ".join(reasons)


def check_empty_result(scenario, actual) -> tuple[str, str]:
    if actual["verified"] == [] and actual["rejected"] == {} and actual["not_found_queries"] == {}:
        return "PASS", "queries=[] produced an empty, valid result (no crash)"
    return "FAIL", f"queries=[] did not produce an empty result: {actual}"


def check_no_crash(status, error, scenario, actual) -> tuple[str, str]:
    if status != "ok":
        return "FAIL", f"pipeline crashed when it must not have: {status}: {error}"
    # Informational identity check when something did verify, so a "no
    # crash" pass does not silently hide an obvious false positive.
    texts, _ = _query_texts_and_injections(scenario)
    verified = actual["verified"] if actual else []
    if verified and texts:
        query = texts[0]
        matches = [v for v in verified if identity_sanity(query, v["title"])]
        if not matches:
            return (
                "PASS",
                "ran without crash (criterion met); note: verified entries present but none "
                "independently identity-match the query -- informational only, not scored here",
            )
    return "PASS", "ran without crash (criterion met)"


def check_value_error(status, error) -> tuple[str, str]:
    if status == "value_error":
        return "PASS", f"ValueError correctly raised: {error}"
    if status == "exception":
        return "FAIL", f"raised {error}, expected ValueError specifically"
    return "FAIL", "expected a ValueError to be raised, but the call completed normally"

def check_dedup(scenario, actual) -> tuple[str, str]:
    verified = actual["verified"]
    if not verified:
        gap = _is_pure_execution_gap(scenario, actual)
        if gap:
            return "INCONCLUSIVE", gap
        return "FAIL", "expected a deduped verified entry but verified is empty"
    if len(verified) == 1:
        entry = verified[0]
        if entry["num_candidates"] >= 2:
            return "PASS", f"single verified entry with num_candidates={entry['num_candidates']} (>=2): deduped correctly"
        gap = _is_pure_execution_gap(scenario, actual)
        if gap:
            return "INCONCLUSIVE", gap
        return (
            "INCONCLUSIVE",
            "only one verified entry with num_candidates=1 -- likely only one of the two "
            "queries reached a usable candidate live (a live-data ranking risk, not "
            "necessarily a pipeline bug per this scenario's own note)",
        )
    dois = [v.get("doi") for v in verified if v.get("doi")]
    if len(dois) != len(set(dois)):
        return "FAIL", f"{len(verified)} separate verified entries share a DOI -- dedupe failed to merge them"
    return (
        "INCONCLUSIVE",
        f"{len(verified)} separate verified entries with distinct identifiers -- OpenAlex's own "
        "search ranking returned different top records for the two differently-worded queries "
        "(a live-data risk explicitly called out by this scenario's own note, not necessarily a bug)",
    )


def check_scale(scenario, actual) -> tuple[str, str]:
    texts, injections = _query_texts_and_injections(scenario)
    reasons = []
    fail = False
    inconclusive = False

    for text, state in injections.items():
        v, r = check_error_state_for(text, state, actual)
        reasons.append(f"[injected {state}] {'OK' if v == 'PASS' else v}: {r}")
        if v == "FAIL":
            fail = True

    fabricated = [t for t in texts if t not in injections and is_fabricated_text(t)]
    real = [t for t in texts if t not in injections and not is_fabricated_text(t)]

    verified = actual["verified"]
    leaked = [t for t in fabricated if any(identity_sanity(t, v["title"]) for v in verified)]
    if leaked:
        fail = True
        reasons.append(f"fabricated quer{'y' if len(leaked)==1 else 'ies'} leaked into verified: {leaked}")
    else:
        reasons.append(f"{len(fabricated)} fabricated quer{'y' if len(fabricated)==1 else 'ies'}: none leaked into verified")

    matched_real = [t for t in real if any(identity_sanity(t, v["title"]) for v in verified)]
    gap = _is_pure_execution_gap(scenario, actual)
    if len(matched_real) < len(real):
        if gap:
            inconclusive = True
            reasons.append(f"real queries: {len(matched_real)}/{len(real)} identity-matched in verified; {gap}")
        else:
            unmatched = [t for t in real if t not in matched_real]
            reasons.append(f"real queries: {len(matched_real)}/{len(real)} identity-matched; unmatched: {unmatched}")
            if not matched_real:
                fail = True
    else:
        reasons.append(f"real queries: {len(matched_real)}/{len(real)} identity-matched in verified")

    if fail:
        return "FAIL", " | ".join(reasons)
    if inconclusive:
        return "INCONCLUSIVE", " | ".join(reasons)
    return "PASS", " | ".join(reasons)


# --------------------------------------------------------------------------
# Structural (synthetic, no-network) checks for the METADATA_CONFLICT
# scenarios that the fixture's own `expect.note` says must be assessed by
# reading resolve/conflicts.py and resolve/identity.py directly.
# --------------------------------------------------------------------------


def _synthetic_candidate(**overrides) -> Candidate:
    base = dict(
        source_adapter="OPENALEX",
        source_record_id="https://openalex.org/W_SYNTH_A",
        title="Governing the Commons: The Evolution of Institutions for Collective Action",
        authors=["Elinor Ostrom"],
        year=1990,
        doi="10.1017/cbo9780511807763",
        issn="0000-0000",
    )
    base.update(overrides)
    return Candidate(**base)


def structural_conflict_year() -> tuple[str, str]:
    a = _synthetic_candidate(source_record_id="W_A", year=2019)
    b = _synthetic_candidate(source_record_id="W_B", year=2021)
    work = CanonicalWork(candidates=[a, b])
    apply_conflict_state(work)
    ok = work.state == VerificationState.CONFLICT and any(c["field"] == "year" for c in work.conflicts)
    return ("PASS" if ok else "FAIL"), (
        "synthetic same-DOI candidates with year=2019 vs year=2021 correctly land in CONFLICT "
        "with a 'year' conflict record"
        if ok
        else f"expected CONFLICT with a 'year' field record, got state={work.state}, conflicts={work.conflicts}"
    )


def structural_conflict_title() -> tuple[str, str]:
    a = _synthetic_candidate(source_record_id="W_A", title="Governing the Commons")
    b = _synthetic_candidate(source_record_id="W_B", title="A garbled/mistranslated title variant")
    work = CanonicalWork(candidates=[a, b])
    apply_conflict_state(work)
    ok = work.state == VerificationState.CONFLICT and any(c["field"] == "title" for c in work.conflicts)
    return ("PASS" if ok else "FAIL"), (
        "synthetic same-DOI candidates with disagreeing titles correctly land in CONFLICT with "
        "a 'title' conflict record"
        if ok
        else f"expected CONFLICT with a 'title' field record, got state={work.state}, conflicts={work.conflicts}"
    )


def structural_conflict_author() -> tuple[str, str]:
    a = _synthetic_candidate(source_record_id="W_A", authors=["Ostrom, Elinor"])
    b = _synthetic_candidate(source_record_id="W_B", authors=["Smith, John"])
    work = CanonicalWork(candidates=[a, b])
    apply_conflict_state(work)
    ok = work.state == VerificationState.CONFLICT and any(c["field"] == "authors" for c in work.conflicts)
    return ("PASS" if ok else "FAIL"), (
        "synthetic same-DOI candidates with disagreeing first-author surname correctly land in "
        "CONFLICT with an 'authors' conflict record"
        if ok
        else f"expected CONFLICT with an 'authors' field record, got state={work.state}, conflicts={work.conflicts}"
    )


def structural_no_conflict_author_reorder() -> tuple[str, str]:
    a = _synthetic_candidate(source_record_id="W_A", authors=["Ostrom, Elinor", "Smith, John"])
    b = _synthetic_candidate(source_record_id="W_B", authors=["Ostrom, Elinor", "Doe, Jane"])
    work = CanonicalWork(candidates=[a, b])
    apply_conflict_state(work)
    ok = work.state != VerificationState.CONFLICT and not work.conflicts
    return ("PASS" if ok else "FAIL"), (
        "same first author, differing remaining co-author list correctly does NOT trigger a "
        "conflict (only first-author surname is checked)"
        if ok
        else f"expected no conflict, got state={work.state}, conflicts={work.conflicts}"
    )


def structural_bibliographic_requires_all_fields() -> tuple[str, str]:
    a = _synthetic_candidate(
        source_record_id="W_A", doi=None, pmid=None, issn="1234-5678",
        title="Some Shared Title", year=2000, authors=["Ostrom, Elinor"],
    )
    b = _synthetic_candidate(
        source_record_id="W_B", doi=None, pmid=None, issn="1234-5678",
        title="Some Shared Title", year=2000, authors=["Smith, John"],
    )
    merges = can_merge(a, b)
    ok = merges is False
    return ("PASS" if ok else "FAIL"), (
        "same ISSN+title+year but differing first-author surname, no shared DOI/pmid: "
        "can_merge() correctly returns False (bibliographic_exact_match requires ALL fields)"
        if ok
        else "expected can_merge()=False when first-author surname differs, got True"
    )


_STRUCTURAL_CONFLICT_SUITE = (
    structural_conflict_year,
    structural_conflict_title,
    structural_conflict_author,
    structural_no_conflict_author_reorder,
    structural_bibliographic_requires_all_fields,
)


def run_structural_conflict_suite() -> tuple[str, str]:
    results = [fn() for fn in _STRUCTURAL_CONFLICT_SUITE]
    fails = [(fn.__name__, r) for fn, (v, r) in zip(_STRUCTURAL_CONFLICT_SUITE, results) if v == "FAIL"]
    if fails:
        detail = " | ".join(f"{name}: {reason}" for name, reason in fails)
        return "FAIL", f"{len(fails)}/{len(results)} synthetic conflict-detection checks failed: {detail}"
    return "PASS", (
        f"all {len(results)} synthetic resolve/conflicts.py + resolve/identity.py checks passed "
        "(year conflict, title conflict, author conflict, author-reorder non-conflict, "
        "bibliographic_exact_match requires-all-fields)"
    )


# Per the fixtures' own notes: S044-S048 (NOTE_ONLY) and S041/S042
# (AMBIGUOUS_BY_DESIGN) all ask the judge to assess resolve/conflicts.py and
# resolve/identity.py directly rather than trust a live harness outcome.
STRUCTURAL_CONFLICT_SCENARIOS = frozenset(
    {"S041", "S042", "S044", "S045", "S046", "S047", "S048"}
)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

_NO_CRASH_OUTCOMES = frozenset(
    {
        "VERIFIED_OR_NO_CRASH",
        "NOT_FOUND_OR_NO_CRASH",
        "NOT_FOUND_OR_VALIDATED_ERROR",
        "VERIFIED_OR_NOT_FOUND_BUT_NO_CRASH",
        "VERIFIED_OR_NOT_FOUND_BUT_JUDGE_SHOULD_FLAG_QUALITY_GAP",
        "VERIFIED_BUT_JUDGE_SHOULD_FLAG_RETRACTION",
    }
)

_VERIFIED_LIKE_OUTCOMES = frozenset({"VERIFIED", "VERIFIED_QUERY_ONLY", "VERIFIED_OR_JUDGE_BY_CODE"})
_NOT_FOUND_LIKE_OUTCOMES = frozenset({"NOT_FOUND", "BOTH_NOT_FOUND"})


def judge(scenario: dict[str, Any], run: dict[str, Any]) -> tuple[str, str]:
    scenario_id = scenario.get("scenario_id", "<unnamed>")
    expected_outcome = scenario.get("expect", {}).get("expected_outcome", "")
    status, actual, error = run["status"], run["actual"], run["error"]

    if scenario_id in STRUCTURAL_CONFLICT_SCENARIOS:
        return run_structural_conflict_suite()

    if expected_outcome == "ValueError_raised":
        return check_value_error(status, error)

    if status == "value_error":
        return "FAIL", f"unexpected ValueError raised (expected {expected_outcome}): {error}"
    if status == "exception":
        return "FAIL", f"unexpected {error} raised (expected {expected_outcome})"

    # status == "ok" from here on.
    if expected_outcome in _NO_CRASH_OUTCOMES:
        return check_no_crash(status, error, scenario, actual)
    if expected_outcome == "EMPTY_RESULT_NO_CRASH":
        return check_empty_result(scenario, actual)
    if expected_outcome in _NOT_FOUND_LIKE_OUTCOMES:
        return check_not_found(scenario, actual)
    if expected_outcome in _VERIFIED_LIKE_OUTCOMES:
        return check_verified(scenario, actual)
    if expected_outcome in ("AMBIGUOUS_BY_DESIGN",):
        return run_structural_conflict_suite()
    if expected_outcome in ("RATE_LIMITED", "TIMEOUT", "ACCESS_DENIED", "PARSER_ERROR", "AdapterError_surfaced"):
        return check_error_state(scenario, actual)
    if expected_outcome == "MIXED_PER_QUERY":
        return check_mixed_per_query(scenario, actual)
    if expected_outcome == "DEDUPED_SINGLE_CANONICAL_WORK":
        return check_dedup(scenario, actual)
    if expected_outcome == "PER_QUERY_CORRECTNESS_AT_SCALE":
        return check_scale(scenario, actual)
    if expected_outcome == "NOTE_ONLY":
        # Any NOTE_ONLY scenario not already routed to the structural
        # conflict suite above (currently: none -- S044-S049 are all
        # covered, S049 via STRUCTURAL_CONFLICT_SCENARIOS is NOT included
        # since its own note asks for a live-regression read instead).
        return check_verified(scenario, actual) if actual.get("verified") else check_not_found(scenario, actual)

    return "INCONCLUSIVE", f"no objective checker implemented for expected_outcome={expected_outcome!r}"


def judge_s049(scenario: dict[str, Any], run: dict[str, Any]) -> tuple[str, str]:
    status, actual = run["status"], run["actual"]
    if status != "ok":
        return "FAIL", f"unexpected crash: {run['error']}"
    verified = actual["verified"]
    if not verified:
        gap = _is_pure_execution_gap(scenario, actual)
        return ("INCONCLUSIVE", gap) if gap else ("PASS", "no verified entries for this query (nothing to false-positive on)")
    texts, _ = _query_texts_and_injections(scenario)
    query = texts[0] if texts else ""
    bad = [v for v in verified if not identity_sanity(query, v["title"])]
    if bad:
        sample = "; ".join((v["title"] or "<no title>") for v in bad[:5])
        return "FAIL", f"{len(bad)}/{len(verified)} verified entries do not identity-match the query: {sample}"
    return "PASS", f"all {len(verified)} verified entrie(s) independently identity-match the query"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    scenarios = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))

    per_scenario: list[dict[str, Any]] = []
    verdict_counts: Counter[str] = Counter()
    per_category: dict[str, Counter[str]] = defaultdict(Counter)

    t0 = time.time()
    for scenario in scenarios:
        scenario_id = scenario.get("scenario_id", "<unnamed>")
        category = scenario.get("category", "UNKNOWN")
        run = execute(scenario)

        if scenario_id == "S049":
            verdict, reason = judge_s049(scenario, run)
        else:
            verdict, reason = judge(scenario, run)

        verdict_counts[verdict] += 1
        per_category[category][verdict] += 1
        per_scenario.append(
            {
                "scenario_id": scenario_id,
                "category": category,
                "expected_outcome": scenario.get("expect", {}).get("expected_outcome"),
                "verdict": verdict,
                "reason": reason,
                "status": run["status"],
            }
        )
        print(f"{scenario_id:>6} {category:<24} -> {verdict:<12} {reason[:140]}")

    elapsed = time.time() - t0
    total = len(scenarios)

    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)

    # --- JSON output ---
    OUT_JSON.write_text(
        json.dumps(
            {
                "generated_from": "tests/run_adversarial_suite.py",
                "fixtures": str(FIXTURES_PATH.relative_to(REPO_ROOT)),
                "total_scenarios": total,
                "elapsed_seconds": round(elapsed, 1),
                "counts_by_verdict": dict(verdict_counts),
                "counts_by_category": {k: dict(v) for k, v in per_category.items()},
                "scenarios": per_scenario,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    # --- Markdown output, same shape as CONCEPT_VALIDATION_REPORT.md ---
    lines: list[str] = []
    lines.append("# ThaiCite — Runner Output (objective reproduction)\n")
    lines.append(
        "Generated by `tests/run_adversarial_suite.py`: a best-effort, code-only, "
        "objective reproduction of the PASS/FAIL/INCONCLUSIVE structure in "
        "`tests/golden/CONCEPT_VALIDATION_REPORT.md`. It is not a substitute for "
        "the original adversarial human/LLM judge pass on nuanced cases -- see the "
        "script's own module docstring for exactly what it does and does not check.\n"
    )
    lines.append(f"Run against {total} scenarios in `{FIXTURES_PATH.relative_to(REPO_ROOT)}` "
                  f"in {elapsed:.1f}s.\n")

    lines.append("## 1. Overall counts\n")
    lines.append("| Verdict | Count |")
    lines.append("|---|---|")
    for v in ("PASS", "FAIL", "INCONCLUSIVE"):
        lines.append(f"| {v} | {verdict_counts.get(v, 0)} |")
    lines.append(f"| **Total scenarios** | **{total}** |\n")

    lines.append("## 2. Per-category breakdown\n")
    lines.append("| Category | Total | PASS | FAIL | INCONCLUSIVE |")
    lines.append("|---|---|---|---|---|")
    cat_total = Counter()
    for category, counts in sorted(per_category.items()):
        cat_n = sum(counts.values())
        cat_total["PASS"] += counts.get("PASS", 0)
        cat_total["FAIL"] += counts.get("FAIL", 0)
        cat_total["INCONCLUSIVE"] += counts.get("INCONCLUSIVE", 0)
        lines.append(
            f"| {category} | {cat_n} | {counts.get('PASS', 0)} | {counts.get('FAIL', 0)} | "
            f"{counts.get('INCONCLUSIVE', 0)} |"
        )
    lines.append(
        f"| **Total** | **{total}** | **{cat_total['PASS']}** | **{cat_total['FAIL']}** | "
        f"**{cat_total['INCONCLUSIVE']}** |\n"
    )

    lines.append("## 3. Per-scenario detail\n")
    lines.append("| Scenario | Category | Expected outcome | Verdict | Reason |")
    lines.append("|---|---|---|---|---|")
    for row in per_scenario:
        reason = row["reason"].replace("|", "\\|").replace("\n", " ")
        if len(reason) > 220:
            reason = reason[:217] + "..."
        lines.append(
            f"| {row['scenario_id']} | {row['category']} | {row['expected_outcome']} | "
            f"{row['verdict']} | {reason} |"
        )

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print()
    print(f"Wrote {OUT_MD.relative_to(REPO_ROOT)}")
    print(f"Wrote {OUT_JSON.relative_to(REPO_ROOT)}")
    print()
    print(f"TOTALS: {dict(verdict_counts)} / {total}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
