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
  session), and ThaiJO, which round 3 (2026-09-20) rebuilt as a
  **Harvester + Local Index split** instead of a per-query OAI-PMH search:
  - `adapters/thaijo_harvester.py::ThaiJOHarvester.sync()` — the real
    network-touching harvest, following `resumptionToken` pagination
    per-endpoint, tracking each endpoint's own `OK` /
    `UNAVAILABLE:<reason>` / `NOT_ATTEMPTED` status independently. Uses the
    **corrected, subdomain-based** OAI-PMH convention
    (`https://{code}.tci-thaijo.org/index.php/index/oai`), not the classic
    `www.tci-thaijo.org/index.php/{code}/oai` path form round 2 assumed
    (and its own test suite baked in) without ever verifying it live.
    **Confirmed this session:** a single gentle `?verb=Identify` probe
    against `https://sc01.tci-thaijo.org/index.php/index/oai` returned
    **HTTP 200** with a valid `<Identify>` response — the corrected
    convention is real, at least for this one code (no other code probed
    this session — see the module's own docstring for the full rationale
    and the deliberately-conservative probing policy).
  - `adapters/thaijo_index.py::ThaiIndex` — a local SQLite+FTS5 snapshot
    the harvester writes into, tokenized through the same shared
    Thai-aware `normalize/tokenize.py` used everywhere else, queried with
    real OR-semantics FTS5 `MATCH` + `bm25()` ranking.
  - `adapters/thaijo.py::ThaiJOAdapter` — now a thin wrapper: `search()`
    queries the local index instantly (no network call per query), and
    returns an honest, clearly-noted `AdapterError` if the index was never
    synced (never silently treated as a source-level `NOT_FOUND`).
  - **Run a harvest before relying on ThaiJO results:**
    `python -m thaicite.adapters.thaijo_harvester --sync`
    (`--codes`, `--max-per-endpoint`, `--db-path` all optional — see that
    module's docstring / `--help`).
  All 5 error states (`NOT_FOUND`/`RATE_LIMITED`/`TIMEOUT`/`ACCESS_DENIED`/
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
  — a deterministic v1 heuristic, not a claim of solved natural-language
  entailment; it returns a label only and never sets a decision itself (see
  `evidence/verifier.py` for the decision logic). Uses real Thai word
  segmentation (round 2). As of **round 3**, a two-stage
  `_interpret_relation()` + `_consistency_check()` design also catches
  semantic-opposite claim/evidence pairs (increase↔decrease, cause↔prevent,
  etc., English and Thai) that previously produced a false `SUPPORTS` with
  no explicit negation word present — **but this is still a finite, fixed
  antonym-pair list, not general entailment**: round 3's own final review
  found two pairs outside the list (accelerate/decelerate;
  Thai ฟื้นฟู/แย่ลง) still silently pass as `SUPPORTS`. See
  `docs/KNOWN_ISSUES.md`'s "Round 3" section for the full honest status —
  **do not treat False-ADMIT-via-semantic-opposite as closed.**
- **Discovery mode vs. identity mode, now a structural guarantee** —
  `core/engine.py`'s `discover_citations()` (used by `find_cites`/CLI
  `find`) is, as of round 3, **incapable of producing an ADMIT/REJECT/HOLD
  decision at the type level**: it returns `DiscoveredCandidate` objects
  that have no `decision` field at all (constructing one with
  `decision=...` raises `TypeError`), and never imports/calls
  `classify_relation()`, `gate_admission_decision()`, or `CiteUse` anywhere
  in its call graph (verified by bytecode inspection in
  `tests/test_discovery_cannot_admit_regression.py`, not just by convention
  or a runtime filter that happened not to trigger). Only `verify_cite`'s
  identity path (`resolve_citations()`, requiring an actual claim) can ever
  produce a `Decision`. `routing/query_planner.py::plan_queries()` is
  genuinely wired into the discovery path.
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
- **Coverage Readout** (`core/coverage.py`, round 3) — `NOT_FOUND` is never
  reported bare: every result carries a per-source (and, for ThaiJO,
  per-endpoint) `OK`/`UNAVAILABLE`/`NOT_ATTEMPTED`/`NOT_CONNECTED` readout
  (`TNRR`/`TCI` always declared `NOT_CONNECTED`, out of v1 scope, rather
  than silently omitted), so "nothing found" always reads as "nothing
  found, given this coverage" — never an implied universal negative.
- **190 offline tests passing** (`PYTHONPATH=src python3 -m pytest tests/ -q`),
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

## Round 3 (same day): a second red-team found round 2 was correct in kind but insufficient — structural redesign

A second, deeper external adversarial test against round 2's fixes found genuine gaps: the
ThaiJO adapter's own endpoint URLs were still wrong (and its own test suite asserted the wrong
format as correct), `ThaiJOAdapter.search()` never actually used the new Thai tokenizer (so the
same 4 real ground-truth papers still failed end-to-end retrieval through the real adapter,
even though round 2's regression test passed — it bypassed the real adapter via a stub),
`find_cites()` could still in principle produce an ADMIT decision (not structurally forbidden),
and — most severe — `classify_relation()` returned a **live-reproduced false `SUPPORTS`** for
directly contradictory claim/evidence pairs using semantic-opposite words instead of explicit
negation. Four structural moves, not more patches: `docs/KNOWN_ISSUES.md`'s "Round 3" section
has the full detail, including the honest remaining limitation on relation classification
(finite antonym-pair list) and the confirmed-live ThaiJO endpoint (`sc01`, HTTP 200).

## Round 4 (same day): problem-centered redesign — claim discipline, evidence-status layer, proposition-based relation, coverage truth

A fourth, deeper red-team confirmed the single most dangerous bug class in the whole
project — evidence merely *about* a claim (an objective/hypothesis sentence) being classified
`SUPPORTS` and reaching `ADMIT` — plus a silent claim-substitution bug and a DOI-weaker-than-
fuzzy-title-match bug. Four structural fixes landed (claim discipline, a new
`evidence/statement_type.py` evidence-status layer, proposition-based relation with a new
`QUALIFIES` label, and a richer coverage-state vocabulary). **Round 4's own final review then
found 2 more real bugs in the fixes themselves** (a clause/negation-blind RESULT-cue matcher
recreating the same bug class one layer down; a DOI exact-match shortcut with no substring-
boundary check) — both fixed same-day. A real, still-open, lower-severity gap (double-negation
mishandling, producing a false REJECT not a false ADMIT) is documented honestly, not fixed.
Full detail: `docs/KNOWN_ISSUES.md`'s "Round 4" section.

**Founder's own assessment after round 4**: this repeating pattern (fix a semantic-judgment
bug → red-team finds the same category of error one layer down) is itself evidence that
deterministic lexical pattern-matching is the wrong tool for genuinely semantic work
(statement typing, proposition extraction, claim↔evidence relation) — planned next redesign
moves this to an AI "Reader" role (shown only `claim`+`passage`, never asked to argue for the
claim, to reduce confirmation bias) and an AI "Scout" role for query/concept expansion
(replacing large synonym/antonym dictionaries), with all of round 4's deterministic logic
demoted to a fallback/checker layer rather than the primary mechanism, and the deterministic
Gate remaining the sole ADMIT/REJECT/HOLD authority regardless. Not yet implemented as of this
note.

## Validation status — read before trusting this build

All four rounds of fixes above are **unit-tested (190/190 passing) but not yet confirmed by a
full live 100-scenario adversarial re-run** — OpenAlex rate-limiting has kept that re-run
incomplete since round 1 (30 PASS / 0 FAIL / 70 INCONCLUSIVE as of the last attempt), and
rounds 2–4 have not been live-tested at that scale at all yet. Per this project's own honesty
tier (`ARCHITECTURE.md` §72's `Dr` label): this is a plausible, carefully-tested
architecture, not yet a "proven better than baseline" result. See `docs/KNOWN_ISSUES.md` and
`tests/golden/` for the full trail.
