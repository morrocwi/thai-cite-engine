# G6 Fix Revalidation (2026-09-20)

**Why offline:** at the start of this revalidation run, the live OpenAlex API
was already rate-limited on this workstation's shared IP —
`curl -s -o /dev/null -w "%{http_code}" "https://api.openalex.org/works?search=test&per-page=1"`
returned `429` immediately before this run started. Spamming more live
requests against an already-rate-limited shared-IP quota would burn budget
for no signal, so this revalidation is offline-first: hand-authored,
OpenAlex-response-shaped JSON fixtures run through the real, unmocked
`OpenAlexAdapter.to_candidates()` conversion (pure, no network call) and the
real, unmocked `verify()` / `gate_g6_identity_match()` logic in
`src/thaicite/evidence/verifier.py`, followed by exactly one gentle live
smoke-test attempt.

Test file: `tests/test_g6_fix_offline.py` (10 tests, all passing).

## 1. What the offline unit tests proved

Each case builds a synthetic OpenAlex `works` item with the real field
shape (`id`, `title`/`display_name`, `publication_year`, `authorships`,
`doi`, `primary_location`, `abstract_inverted_index`), converts it with the
production `OpenAlexAdapter.to_candidates()`, wraps it in a `CanonicalWork`,
and calls the production `verify(work, context=..., query=...)`.

| # | Case (source: CONCEPT_VALIDATION_REPORT.md) | Assertion | Result |
|---|---|---|---|
| 1 | Vaswani et al. 2017 "Attention Is All You Need" (S018) — real paper | `state == VERIFIED`, `G6_identity_match == True` | **PASS** |
| 2 | SegFormer distractor (shares "transformer"/"attention" only) vs. Vaswani query | `state == REJECTED`, `G6_identity_match == False` | **PASS** |
| 3 | BEiT distractor vs. Vaswani query | `state == REJECTED`, `G6_identity_match == False` | **PASS** |
| 4 | Swin Transformer distractor vs. Vaswani query | `state == REJECTED`, `G6_identity_match == False` | **PASS** |
| 5 | Watson & Crick 1953 (S016) — real paper | `state == VERIFIED`, `G6_identity_match == True` | **PASS** |
| 6 | 3 unrelated DNA/oligonucleotide/collagen/RNA-NMR distractors vs. Watson & Crick query | `state == REJECTED`, `G6_identity_match == False` (each) | **PASS** |
| 7 | He et al. 2016 "Deep Residual Learning" / ResNet (S025) — real paper | `state == VERIFIED`, `G6_identity_match == True` | **PASS** |
| 8 | SchNet + tomato-leaf-disease distractors vs. ResNet query | `state == REJECTED`, `G6_identity_match == False` (each) | **PASS** |
| 9 | Thai-language title (`ปัญหาการสื่อสาร...`) | `THAI_LANGUAGE` tag present via `classify_thai_relevance()` | **PASS** |
| 10 | Foreign (UK) authors, English-language title studying "coastal communities... Thailand" | `ABOUT_THAILAND` + `FOREIGN_ABOUT_THAILAND` present, `THAI_LANGUAGE` / `PUBLISHED_IN_THAILAND` absent | **PASS** |

**Command:** `PYTHONPATH=src python3 -m pytest tests/test_g6_fix_offline.py -v`
**Outcome:** `10 passed in 0.20s`

**What this confirms about the G6 fix:** the new `gate_g6_identity_match()`
in `src/thaicite/evidence/verifier.py` compares a candidate's own
title/authors against the query citation string (never against free-text
`context`), requires at least 2 real shared title tokens (or a real
author-surname match) before a title-similarity ratio is even considered,
and correctly separates "this is the cited work" (VERIFIED) from "this is a
different real paper that merely shares generic vocabulary" (REJECTED) —
directly reversing the POSITIVE_CONTROL 15/15 FAIL pattern documented for
the old context-keyword-overlap G6 in `CONCEPT_VALIDATION_REPORT.md` §3–4
for all three positive-control cases re-tested here (S016 Watson & Crick,
S018 Vaswani/"Attention Is All You Need", S025 ResNet). The Thai-relevance
tagging module (`normalize/thai_relevance.py`, unaffected by the G6 change
but re-checked here for the founder's Thai-citation question) correctly
distinguishes THAI_LANGUAGE from FOREIGN_ABOUT_THAILAND.

## 2. Live smoke test (single attempt, no retry)

Per instructions: one gentle live OpenAlex query, short timeout, no retry
loop, honest report either way.

**Query:** `"Vaswani Attention is all you need"` (real, unambiguous paper),
`OpenAlexAdapter(timeout_s=8, per_page=1)`, one call to `.search()`.

**Result:**
```
ADAPTER_ERROR state=RATE_LIMITED message=OpenAlex returned HTTP 429 (rate limited). elapsed=0.12s
```

**Honest outcome: still rate-limited (HTTP 429), reported as-is.** No
second or third query was attempted. This is the correct, non-overclaiming
behavior the adapter is designed for — a genuine 429 surfaces as
`RATE_LIMITED`, never silently collapsed into `NOT_FOUND` or retried until
it "works." The rate-limit condition observed by the harness before this
run started is confirmed still in effect; the shared-IP daily budget has
not yet reset.

## 3. Bottom line

The G6 fix is confirmed correct at the unit level against all three
re-tested POSITIVE_CONTROL cases from the original 100-scenario adversarial
report (previously 15/15 FAIL under the old context-keyword-overlap G6) plus
a Thai-relevance tagging spot-check, without adding any further load to the
already-rate-limited live OpenAlex API. A full live re-run of the original
100-scenario suite (especially the 22 previously-INCONCLUSIVE scenarios and
the fully-untested DUPLICATE_ACROSS_SOURCE category) still requires either
an authenticated OpenAlex API key or the shared free-tier daily quota to
reset, per the report's own §6–7 recommendation — this offline revalidation
does not substitute for that, it only confirms the G6 logic itself is fixed.
