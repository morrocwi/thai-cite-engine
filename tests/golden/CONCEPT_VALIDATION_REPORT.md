# Thai Cite Engine — Concept Validation Report

**Scope:** 100-scenario adversarial concept-validation run of the Thai Cite Engine
prototype. This prototype covers the OpenAlex adapter plus the core
identity-resolution / conflict / verification firewall logic only — it does not
cover the full planned adapter set (Crossref, Google Scholar, Thai journal
indexes, etc.).

**Run conditions:** All scenarios were executed for real (`run_scenario()`)
against the live, unmocked code, including genuine live HTTP calls to the
OpenAlex API where reachable. A meaningful fraction of the run window fell
inside an OpenAlex shared-IP free-tier daily rate limit (HTTP 429), which
produced `INCONCLUSIVE` verdicts for scenarios that could not reach a
candidate-fetch or merge/verify code path; these are execution gaps, not code
defects, and are reported honestly as `INCONCLUSIVE` rather than guessed.

---

## 1. Overall counts

| Verdict | Count |
|---|---|
| PASS | 54 |
| FAIL | 24 |
| INCONCLUSIVE | 22 |
| **Total scenarios** | **100** |

Of the 24 `FAIL` verdicts, **23 carry `violation_severity: critical`** and
1 carries `violation_severity: minor` (S065, a latent Thai tone/vowel-mark
normalization gap surfaced by targeted unit testing after the live run itself
was rate-limited).

## 2. Per-category breakdown

| Category | Total | PASS | FAIL (critical) | FAIL (minor) | INCONCLUSIVE |
|---|---|---|---|---|---|
| FABRICATED_WORK | 15 | 14 | 1 | 0 | 0 |
| POSITIVE_CONTROL | 15 | 0 | 15 | 0 | 0 |
| SEMANTIC_TRAP | 10 | 8 | 2 | 0 | 0 |
| METADATA_CONFLICT | 10 | 6 | 4 | 0 | 0 |
| ERROR_STATE | 10 | 9 | 1 | 0 | 0 |
| THAI_SPECIFIC | 10 | 5 | 0 | 1 | 4 |
| DUPLICATE_ACROSS_SOURCE | 10 | 0 | 0 | 0 | 10 |
| BOUNDARY_EDGE | 10 | 9 | 0 | 0 | 1 |
| OTHER | 10 | 3 | 0 | 0 | 7 |
| **Total** | **100** | **54** | **23** | **1** | **22** |

## 3. Headline finding

The engine's basic "reject a purely invented citation" behavior is sound:
**FABRICATED_WORK (14/15 PASS) and SEMANTIC_TRAP (8/10 PASS)** confirm that a
citation with an invented author/title/journal that genuinely returns zero
OpenAlex hits is correctly routed to `not_found_queries` with honest,
non-overclaiming wording ("not found via the adapters searched, not that the
work does not exist"), and error states (`RATE_LIMITED`/`TIMEOUT`/
`ACCESS_DENIED`/`PARSER_ERROR`) are correctly kept distinct from `NOT_FOUND`
(ERROR_STATE 9/10 PASS).

However, adversarial testing surfaced a **systemic, architectural defect** in
gate **G6 ("context relevance")**, in `src/thaicite/evidence/verifier.py`:
G6 checks keyword overlap between the scenario's free-text `context` field (or,
worse, the harness's own scenario-metadata prose) and a candidate's
title/abstract. **No gate anywhere in the pipeline (G1–G7) ever compares a
returned candidate's title/author/year against the actual citation string
being verified.** Combined with a stopword list that omits very common,
low-information words ("this", "not", "one", "two", "work", "via", "where",
"only", "real", "same"), this means:

- Any OpenAlex hit sharing even a single generic word with the free-text
  context clears relevance and gets stamped `VERIFIED`.
- This happens **whether or not the returned work is the work actually being
  cited.**

The clearest evidence is the **POSITIVE_CONTROL category: 15/15 FAIL.**
Every well-known, real, unambiguously-indexed positive-control paper tested
(Watson & Crick 1953, Kahneman & Tversky 1979, Vaswani et al. 2017 "Attention
Is All You Need", Porter 1985, Diener 1984, Rosling, the COCO paper, Ostrom
1990, Sen 1999, He et al. 2016 ResNet, Banerjee & Duflo, WHO 2020, Bandura
1977, Devlin et al. 2019 BERT, Darwin 1859) triggered the same failure mode:
either the real cited work silently vanished from every output bucket while
unrelated real papers were confidently marked `VERIFIED` in its place, or the
real work was correctly verified but sat alongside several unrelated
false-positive `VERIFIED` entries for the same query (e.g. S023).

This is **more dangerous than a clean rejection or `NOT_FOUND`**: every
false-positive `VERIFIED` record carries a real, traceable DOI, so a
downstream user has no signal that the "verified" citation does not actually
correspond to what was cited. It also proved reproducible in the
`SEMANTIC_TRAP` and `METADATA_CONFLICT` categories once judges probed past the
literal scenario text (S031, S032, S041–S043, S049), and once more under a
genuine live 429 in `ERROR_STATE` (S060) — i.e. it is not confined to one
category, it is a property of G6 itself.

## 4. All CRITICAL findings (23)

### S008 — FABRICATED_WORK
The literal scenario query (full citation string) genuinely dodged the bug
(zero OpenAlex hits, correct `NOT_FOUND`). But re-querying the *same*
fabricated citation by title alone — a trivial fallback any real pipeline or
researcher would try — returned 10 unrelated real papers, and G6 (pure
keyword-overlap against `context`, not against the citation) let all 10 pass
and be marked `VERIFIED`. Root cause: G6 never checks that a returned record
corresponds to the citation being verified, and its stopword list is missing
common words ("this", "about", "work", "adult").

### S016 — POSITIVE_CONTROL
Watson & Crick (1953), real DOI `10.1038/171737a0`, never appears anywhere in
the output. Instead 8 unrelated real papers (DNA/oligonucleotide-adjacent,
sharing keywords like "nucleic"/"acid") are marked `VERIFIED` for this query.

### S017 — POSITIVE_CONTROL
Kahneman & Tversky (1979) "Prospect Theory" (real DOI `10.2307/1914185`)
never appears in the output. 8 different unrelated real decision-theory papers
are marked `VERIFIED` instead, because OpenAlex's fulltext search on the whole
citation string returns 22,549 loosely related hits and G6 has no
candidate-vs-query identity check.

### S018 — POSITIVE_CONTROL
Vaswani et al. (2017) "Attention Is All You Need" never appears anywhere in
the output. 8 unrelated papers (SegFormer, BEiT, Swin Transformer, MolTrans,
etc.) are marked `VERIFIED`, matched only on shared buzzwords ("transformer",
"attention", "paper", "need").

### S019 — POSITIVE_CONTROL
Porter (1985) "Competitive Advantage" never appears in verified/rejected/
not_found. All 10 OpenAlex hits (ESG performance, supply-chain risk, IT
investment — none Porter's book) are marked `VERIFIED`.

### S020 — POSITIVE_CONTROL
Diener (1984) "Subjective well-being" (real DOI `10.1037/0033-2909.95.3.542`)
never appears. 10 unrelated real papers are marked `VERIFIED`, one matched on
the single shared word "psychological." The scenario's own shallow expect
check ("outcome == VERIFIED") superficially passes since `verified` is
non-empty — but none of the 10 entries is the cited work.

### S021 — POSITIVE_CONTROL
Rosling's "Factfulness" (2018) never appears in the output. 5 unrelated real
papers are marked `VERIFIED` via loose keyword overlap ("global", "health",
"literature", "known") with the free-text context.

### S022 — POSITIVE_CONTROL
The actual COCO paper (Lin et al.) never appears in verified/rejected/
not_found — it is silently dropped. 6 unrelated real papers are marked
`VERIFIED`, matched only on stray generic words ("work"/"well") from the
context sentence.

### S023 — POSITIVE_CONTROL
Ostrom (1990) "Governing the Commons" (real record `W2742391459`) *is*
correctly verified — but 8 additional, unrelated real works (Social Capital
and Collective Management of Resources, Governing the Hollow State, etc.) are
also marked `VERIFIED` for the same single query, admitted purely because G6
requires only one shared token ("action", "well", "governing",
"institutions"). A caller reading `verified` cannot tell 8 of 9 entries are
not the cited work.

### S024 — POSITIVE_CONTROL
Sen (1999) "Development as Freedom" returns 7 `VERIFIED` citations — the
correct work plus 6 unrelated papers (including a materials-science ReaxFF
force-field paper), all cleared by a single shared generic keyword
("development"/"freedom").

### S025 — POSITIVE_CONTROL
He et al. (2016) "Deep Residual Learning for Image Recognition" (ResNet,
real DOI `10.1109/CVPR.2016.90`) never appears anywhere in the output. 10
unrelated real papers (plant-disease detection, EEG decoding, SchNet,
Grad-CAM, crack-damage detection) are marked `VERIFIED` instead, on one of the
hardest, most unambiguous positive controls in deep learning.

### S026 — POSITIVE_CONTROL
Banerjee & Duflo "Poor Economics" (2011) never appears in OpenAlex's own
results for this query (confirmed independently) or in the engine's output.
10 unrelated real papers are marked `VERIFIED` anyway via single-keyword
overlap ("work", "well", "based", "rct", "poor").

### S027 — POSITIVE_CONTROL
WHO (2020) global diabetes-prevalence report — 10 unrelated works (dementia
care, oral disease, cancer statistics, obesity, a mental-health survey
instrument) are marked `VERIFIED` for this single query via generic-token
overlap ("global", "health", "prevalence", "world").

### S028 — POSITIVE_CONTROL
Bandura (1977) — real DOI `10.1037/0033-295X.84.2.191` absent from the
output. All 10 returned candidates are marked `VERIFIED` despite none being
the actual cited paper.

### S029 — POSITIVE_CONTROL
Devlin et al. (2019) BERT/NAACL paper never appears anywhere in the output.
8 unrelated papers (TinyBERT, LXMERT, SciBERT, BERTScore, BLIP, CodeT5, etc.)
are marked `VERIFIED` instead. Note: a genuine cross-source METADATA_CONFLICT
(two differently-titled T5 records) was correctly blocked from `VERIFIED` in
the same run, showing G7/conflict-handling itself is sound — the defect is
specifically G6's context-vs-candidate (rather than query-vs-candidate)
comparison.

### S030 — POSITIVE_CONTROL
Darwin's "On the Origin of Species" (1859) never appears in the output. 7
unrelated real records (evolutionary-narrative literary criticism, speciation
ecology papers, a Darwin Tree of Life genomics project, etc.) are marked
`VERIFIED`, admitted because the context sentence itself contains "Darwin",
"1859", "Origin", "Species" and any candidate sharing one such token clears
G6.

### S031 — SEMANTIC_TRAP
The literal scenario text technically passed only because the harness's own
"Fake near-paraphrase:" test label, baked into the query string, happened to
drive OpenAlex to zero results — a scenario-construction artifact, not a
firewall success. Stripping that label from the same fabricated
Kahneman/Tversky-paraphrase citation causes an unrelated real paper (Ghana
malaria/diarrhoea mortality) to be marked `VERIFIED`, matched only on trivial
shared words ("real", "work", "not", "literature", "risk").

### S032 — SEMANTIC_TRAP
Same pattern: stripping the harness's "Fake near-paraphrase:" label from a
fabricated Liang & Petrov citation causes 4 unrelated real papers (hydrogel
drug delivery, magnetoelectric materials, exosome regeneration, an unrelated
attention survey) to be marked `VERIFIED`, matched on single generic keywords
("need", "all", "attention").

### S041 — METADATA_CONFLICT
No genuine cross-source CONFLICT state was exercised (as the scenario itself
predicted), but the live run showed the real "Deep Residual Learning" (ResNet)
record correctly REJECTED at G6 — while 10 topically unrelated real works
(AlphaFold, EEG decoding, plant-disease detection, Faster R-CNN, Wide & Deep
Recommenders, Contrastive Predictive Coding) were marked `VERIFIED`, justified
only by single-token matches on extremely generic words the stopword list
misses ("one", "two", "work", "via", "where", "same").

### S042 — METADATA_CONFLICT
No false merge/CONFLICT occurred between two genuinely different
Kahneman-coauthored works (correct, narrow claim holds). But 11 topically
unrelated OpenAlex papers (implicit bias in physicians, consumer-judgment
metacognition, cognitive biases in medical decisions) were marked `VERIFIED`
via coincidental overlap with ordinary context words ("that", "into", "not",
"them", "but", "real", "two", "pair"), while the genuinely on-topic Prospect
Theory candidates that were actually returned for the same queries were
rejected at G6 for lacking that coincidental overlap.

### S043 — METADATA_CONFLICT
No context-based author-identity contamination occurred (the scenario's own
narrow claim holds). But OpenAlex's own search never returns the real Vaswani
et al. 2017 paper for this query, and G6 then rubber-stamps 4 unrelated real
papers (TransUNet, an attention-mechanisms survey, HISTORIAE, FFA-Net) as
`VERIFIED`, matched on meaningless shared tokens ("this", "same", "into",
"but"). The real paper is silently absent from every bucket.

### S049 — METADATA_CONFLICT
For the query "Machine Learning for Thai text classification benchmark," 6 of
10 candidates with zero topical relation (quantum-circuit ML models, AutoML,
an image-augmentation survey) are marked `VERIFIED`, matched via ultra-common
words the stopword list omits ("this", "not", "only", "over", "real",
"source").

### S060 — ERROR_STATE
Per-query error isolation is confirmed correct (query 1's injected
`RATE_LIMITED` did not suppress query 2). But query 2's real target —
Kahneman & Tversky (1979) — never appears anywhere in the output. 4 unrelated
real papers (Salience Theory of Choice Under Risk, Frames of Mind in
Intertemporal Choice, etc.) are marked `VERIFIED` instead, because G6 matches
against the scenario's free-text meta-description rather than the actual
citation content.

## 5. Minor finding (non-critical)

### S065 — THAI_SPECIFIC (`violation_severity: minor`)
The live run itself was correctly handled (real OpenAlex 429 → `RATE_LIMITED`,
not collapsed into `NOT_FOUND`, no fabrication). Targeted unit testing of
`identity.py::_normalize_title` afterward found that Python's `\w` does not
match Thai combining tone/vowel marks (Unicode category Mn), so
`_normalize_title` silently strips them as if they were punctuation (e.g.
`'ปัญหาการสื่อสาร...'` → `'ปญหาการสอสาร...'`). This feeds both the
`_values_conflict('title', …)` check used by the METADATA_CONFLICT gate and
`bibliographic_exact_match` used for identity merging, so two Thai titles that
differ only by tone/vowel marks (a real semantic difference) would silently
normalize identically. This is a latent defect surfaced by adversarial code
inspection, not an observed live fabrication in this run.

## 6. Execution gaps (INCONCLUSIVE, 22 scenarios)

22 scenarios could not reach the behavior actually under test because the
live OpenAlex API returned genuine HTTP 429s during the run window — the
shared free-tier IP daily budget was exhausted (confirmed independently via
direct `curl` to `api.openalex.org`), not a code defect. In every one of these
22 cases, the error-handling path that *did* execute behaved correctly
(`RATE_LIMITED`/`TIMEOUT` surfaced honestly under `rejected`, never collapsed
into `NOT_FOUND`, nothing fabricated under `verified`). Notably, the entire
**DUPLICATE_ACROSS_SOURCE category (10/10) is untested** — cross-adapter
identifier-based dedup/merge behavior has zero live confirmation from this
run. These 22 scenarios should be re-run with an OpenAlex API key (or after
the shared daily budget resets) before any release decision treats them as
passed.

## 7. Go/No-Go recommendation

**NO-GO** on proceeding directly to the full v0.1 build (expanding to the
full planned adapter set) without first fixing the root cause below.

**Rationale:**

1. **This is a single, root-cause architectural defect, not a set of
   unrelated bugs.** Gate G6 in `src/thaicite/evidence/verifier.py` compares
   free-text `context`/scenario metadata against a candidate's title/abstract
   via weak, single-keyword overlap, and no gate in the G1–G7 chain ever
   compares a candidate's own title/author/year against the citation string
   actually being verified. Building out more source adapters (Crossref,
   Google Scholar, Thai journal indexes, etc.) on top of this gate would only
   multiply the surface area for the same false-`VERIFIED` failure mode —
   every new adapter's real-but-unrelated hits would clear G6 the same way.
2. **POSITIVE_CONTROL failed 15/15.** A citation-verification tool that
   cannot reliably attach `VERIFIED` to the actual paper being cited — for
   any of 15 canonical, unambiguous, real-world citations — has not yet
   demonstrated its core value proposition. This is judged more severe than
   a correctly-behaving rejection path would be, because every false
   `VERIFIED` entry carries a real, traceable identifier, creating false
   confidence rather than an honest gap.
3. **The fabrication-rejection half of the firewall is genuinely solid**
   (FABRICATED_WORK 14/15, SEMANTIC_TRAP 8/10, ERROR_STATE 9/10, and the
   `NOT_FOUND`-wording / error-state-separation invariants held cleanly
   throughout, including under real live 429s). This half does not need to
   be rebuilt — only the G6 relevance/identity gate does.
4. **DUPLICATE_ACROSS_SOURCE is entirely unverified** (0/10 executed) due to
   live rate-limiting, and should be exercised with a working API key before
   any cross-adapter merge logic in the v0.1 build is trusted.

**Recommended path before v0.1:**
- Redesign G6 (or add a new gate) to compare each candidate's own
  title/authors/year against the query citation's title/authors/year —
  not against free-text `context` — using a real bibliographic-similarity
  check, not single-token overlap.
- Tighten or replace the stopword list; a keyword-overlap signal of any kind
  should not be the sole relevance gate.
- Re-run the full 100-scenario suite (at minimum the 22 INCONCLUSIVE
  scenarios) with an authenticated OpenAlex API key to remove the rate-limit
  execution gap, with particular attention to DUPLICATE_ACROSS_SOURCE.
- Re-run POSITIVE_CONTROL and FABRICATED_WORK/SEMANTIC_TRAP after the G6 fix
  to confirm the fix does not regress the currently-solid rejection behavior.
