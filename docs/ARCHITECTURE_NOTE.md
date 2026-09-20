# Architecture note — v1 build status (2026-09-20, post-expansion)

> **Status update:** this note originally described a narrower v0.1
> concept-validation prototype (OpenAlex-only). The system has since grown
> substantially in the same session — see "What's actually implemented now"
> below. The core invariants this note originally set out to test remain
> unchanged and unweakened throughout that growth.

This is ThaiCite's v1 build. Its core purpose is still to prove
the "AI never becomes the source" invariant holds under real (not mocked)
conditions: every `Candidate` requires a real `source_adapter` +
`source_record_id`, identity merging (`resolve/identity.py`) only ever fuses candidates via
an exact persistent identifier (DOI/PMID/PMCID/arXiv) or an exact ISSN+title+year+author
bibliographic match — never via `similarity_score()`, which exists only for ranking —
metadata disagreement between sources is surfaced as `METADATA_CONFLICT` rather than
silently resolved (`resolve/conflicts.py`), and a `CanonicalWork` reaches `VERIFIED` only
after passing all of gates G1–G7 (`evidence/verifier.py`), never before.

## What's actually implemented now

- **4 real adapters**: `adapters/openalex.py`, `adapters/crossref.py`,
  `adapters/pubmed.py` (all live-verified against their real APIs this
  session), and `adapters/thaijo.py` (OAI-PMH client is implemented and
  correctly distinguishes a real transport failure from `NOT_FOUND`, but its
  base endpoint URL is an **unverified assumption** — ThaiJO's classic
  `/index.php/index/oai` path returned HTTP 404 when checked live; see the
  module's own docstring before trusting live ThaiJO results). All 5 error
  states (`NOT_FOUND`/`RATE_LIMITED`/`TIMEOUT`/`ACCESS_DENIED`/
  `PARSER_ERROR`) stay distinct across every adapter.
- **Citation-Use model** (`core/models.py`: `CiteUse`, `EvidenceLevel`,
  `RelationLabel`, `ContextContract`) — verification is now keyed by
  (work, claim), not by work alone; the same source can `ADMIT` for one claim
  and `REJECT`/`HOLD` for another.
- **3-way `ADMIT`/`REJECT`/`HOLD` gate** (`evidence/verifier.py`'s
  `gate_admission_decision`) layered on top of the unchanged G1–G7 state
  machine — never a replacement for it. **As of round 2 (below), this is now
  the gate that actually controls public output** — `core/engine.py` filters
  the `verified`/`citations` list strictly by `decision == ADMIT`; a `HOLD`
  or `REJECT` decision can no longer leak into "safe to cite" output.
- **Relation classification** (`evidence/relation.py`'s `classify_relation`)
  — a deterministic v1 heuristic (negation/contrast-marker detection), not a
  claim of solved natural-language entailment; it returns a label only and
  never sets a decision itself (see `evidence/verifier.py` for the
  decision logic). Now uses real Thai word segmentation (see round 2 below).
- **Discovery mode vs. identity mode** (`core/engine.py`'s
  `discover_citations()` vs. `resolve_citations()`) — `find_cites`/CLI `find`
  use discovery mode (relevance-gated, genuinely calls
  `routing/query_planner.py::plan_queries()` for a support+challenge query
  family); `verify_cite` uses identity mode (strict candidate-vs-citation
  bibliographic match). These are deliberately different gates for
  deliberately different questions — see round 2 below for why this split
  exists.
- **Thai-first Source Router** (`routing/router.py`) — ThaiJO is ordered
  first for `THAI`/`THAI_HEALTH`/`GENERAL` domains (included by default even
  with no positive Thai signal, per design intent), and is the one deliberate
  exception omitted only for pure `HEALTH`-domain queries with a confirmed
  negative Thai signal. Reports `track_status` honestly when a track is
  degraded rather than hiding a partial failure.
- **Support × Challenge query planner** (`routing/query_planner.py`) —
  deterministic query-family generation; a challenge query is never a literal
  negation of its paired support query. Genuinely wired into discovery mode
  as of round 2.
- **Real Thai tokenization** (`normalize/tokenize.py`, round 2) — uses
  `pythainlp` for Thai/mixed text (a **required** dependency, see
  `pyproject.toml`); pure non-Thai text keeps the original regex behavior
  unchanged.
- **CLI** (`thaicite find --context "..." [--debug]`) and an **MCP server**
  (`mcp_server.py`, genuinely functional in this environment, exposing
  `find_cites`/`verify_cite`).
- **106 offline tests passing** (`PYTHONPATH=src python3 -m pytest tests/ -q`),
  none requiring live network access.

Contact-email parameters (`THAICITE_CONTACT_EMAIL`, optional
`THAICITE_NCBI_API_KEY`) are read from environment variables only — never
hardcoded — with an automated regression test (`tests/test_no_email_leak.py`)
enforcing this.

**Known critical defect, found and (redesigned, not yet fully live-reverified) fixed:** the
first 100-scenario adversarial concept-validation run (2026-09-20) found that the original
relevance gate G6 compared free-text `context` against a candidate's title/abstract by weak
keyword overlap instead of checking the candidate against the citation actually being
verified — every one of 15 POSITIVE_CONTROL scenarios (real, well-known papers) failed,
with unrelated papers confidently marked `VERIFIED` in their place. `evidence/verifier.py`
was redesigned so G6 now checks candidate identity against the query citation string
itself (see that file's module docstring), but a clean, rate-limit-free live 100/100
re-run has not yet completed. Read `docs/KNOWN_ISSUES.md` before treating this prototype
as validated — it links both the original findings
(`tests/golden/CONCEPT_VALIDATION_REPORT.md`) and the current, partially-rate-limited
revalidation status (`tests/golden/RUNNER_OUTPUT_LATEST.md`).

Explicitly **still out of scope** (this list has shrunk since the version of this note
written earlier today — Crossref, ThaiJO, PubMed, and domain routing have since moved from
"deferred" to "implemented, see above"): PMC full-text fetching, TNRR/TCI/TDC/DataCite
adapters; the Thai normalization layer (พ.ศ./ค.ศ. conversion, Thai↔English title matching —
this is a known, not-yet-tested risk: a Thai-language citation whose indexed record has an
English-translated title would currently show zero title-token overlap in the G6 gate, see
`docs/KNOWN_ISSUES.md`); Thai author-name Romanized-form matching; candidate ranking beyond
the raw `similarity_score()` helper; citation formatting/output-style rendering (APA7 etc.);
Source Safety / retraction checking; and the TCITE canonical Work ID (deliberately paused,
see ARCHITECTURE.md §59).

## Round 2 (same day): external adversarial Thai-language test found and fixed 4 more severe bugs

A real external adversarial test (Thai query, real ThaiJO ground-truth papers on Islamic
family law) found the system FAILED to surface any of 4 genuinely relevant, real papers.
Root-caused to 4 distinct, independently-verified bugs — the `VERIFIED`/`ADMIT` semantic
leak (public output wasn't actually filtered by the ADMIT/REJECT/HOLD gate), `find_cites()`
misusing the strict identity gate for broad discovery, a Thai tokenizer that collapsed whole
unspaced Thai sentences into one token, and a ThaiJO adapter capped at 50 records with no
real pagination. All 4 fixed and covered by new regression tests using the exact real
ground-truth titles. Full detail, including what's still honestly unresolved (ThaiJO endpoint
reachability, the still-unimplemented multi-concept Query Planner from ARCHITECTURE.md §9):
**`docs/KNOWN_ISSUES.md`**.

## Validation status — read before trusting this build

Both rounds of fixes above are **unit-tested (106/106 passing) but not yet confirmed by a
full live 100-scenario adversarial re-run** — OpenAlex rate-limiting has kept that re-run
incomplete since round 1 (30 PASS / 0 FAIL / 70 INCONCLUSIVE as of the last attempt), and
round 2 has not been live-tested at that scale at all yet. Per this project's own honesty
tier (`ARCHITECTURE.md` §72's `Dr` label): this is a plausible, carefully-tested
architecture, not yet a "proven better than baseline" result. See `docs/KNOWN_ISSUES.md` and
`tests/golden/` for the full trail.
