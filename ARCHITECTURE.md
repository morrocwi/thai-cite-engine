# ThaiCite — Architecture

> Status: design document. **Two parts, one system.** Part I (§0–43) is the
> full long-term target architecture, as originally briefed. Part II (§44–65)
> is a deliberate later narrowing of *scope and internal verification model*
> after further review — it is the current direction for what actually gets
> built first (v1). §43a gives a term-by-term map between the two so nothing
> reads as contradictory. A separate concept-validation prototype (core
> firewall logic + one real adapter, stress-tested against 100 adversarial
> scenarios) is being built and tested in parallel against Part I's shape —
> see `tests/golden/CONCEPT_VALIDATION_REPORT.md` once available, and
> `docs/HANDOFF_2026-09-20.md` for that run's status.

## 0. Naming and explicit exclusions

"ThaiCite" is a **temporary/working name** (ชื่อชั่วคราว), not
necessarily final.

The architecture is deliberately locked narrow. This system is explicitly
**NOT**:
- an "OpenAlex for Thailand" (not a competing general-purpose Thai research
  database),
- a knowledge graph for end users,
- related to Glosa (a separate methodology in this workspace — no
  relationship intended or implied).

It is a standalone Git repository that does exactly one job well.

## 1. What this system is

ThaiCite is a standalone system with one job:

> **Context in → Verified citation list out.**

It is not a writing tool, not a general chatbot, and not an attempt to replace
OpenAlex, PubMed, or Thailand's own research databases. It is a middle layer that
connects the existing scholarly ecosystem — searching, verifying, deduplicating,
and selecting citations that are actually relevant to a user's real context.

Internally it may search multiple databases, work across Thai and English, walk
citation graphs, open abstracts or full text, check DOIs/PMIDs, and merge
duplicate records — but the output the user receives should be as simple as
possible: a list of citations ready to check or use.

The system draws on multiple source types by role, never treating any single
source as the whole answer. OpenAlex is used for discovery and the citation
graph; Crossref and DataCite for persistent-identifier and metadata checks;
PubMed/PMC for a dedicated biomedical path; Thai work connects to ThaiJO, TNRR,
TCI, and TDC/ThaiLIS through each system's own official interface.

## 2. Why this system matters

The core problem is not that AI is bad at finding papers. It is that AI can
fabricate a highly plausible-looking title, author, DOI, publication year, or
citation detail for a work that does not exist. Even when a paper is real, it
may not actually support the claim the AI is attaching it to.

The system is therefore designed around one governing principle:

> **AI is not a source.**

AI may help interpret context, expand search terms, translate Thai↔English
concepts, choose which databases to search, and rank candidates. AI has no
authority to invent a citation record, fabricate a DOI/PMID/title/author/year/
journal, or declare a citation "verified" purely from model confidence.

Every citation must originate from a record actually pulled from a real
source, and every candidate must be able to answer: *which database, which
record ID, when was it retrieved, and what is its origin.*

The anti-hallucination mechanism is therefore not a prompt instruction ("don't
make things up") — it is enforced as a software-architecture requirement.

## 3. Baseline pipeline

```text
Context
→ Query planning
→ Search real scholarly databases
→ Receive real source records
→ Resolve identity
→ Deduplicate
→ Fetch actual metadata/content
→ Check relevance
→ Verification gates
→ Citation formatting
→ Verified citation list
```

If AI produces a realistic-sounding title with no matching record in OpenAlex,
Crossref, PubMed, ThaiJO, or any other supported database, that title can never
become a citation object and can never reach the final output:

```text
No source record   → No candidate
No candidate        → No canonical work
No canonical work   → No VERIFIED state
No VERIFIED state   → No right to appear in the citation list
```

This chain is the system's most important hallucination firewall.

**Discovery is not verification.** OpenAlex or semantic search may say a work
looks highly relevant to a context, but semantic similarity has no authority to
confirm a work's identity. Merging records requires a checkable identifier —
DOI, PMID, PMCID, repository ID, or checkable bibliographic evidence.

**"Not found" is not "does not exist."** The system must distinguish at least
NOT_FOUND, RATE_LIMITED, TIMEOUT, ACCESS_DENIED, and PARSER_ERROR — an API
outage or a missing record in one database does not prove a work does not
exist.

## 4. Why Thailand needs an extra layer

Thai research spans many ecosystems and has problems that general international
systems handle poorly: Thai↔English titles for the same work, multiple name
forms for one researcher, multiple forms of Thai university names, พ.ศ.↔ค.ศ.
year conversion, works without a DOI, theses/reports not indexed in a journal,
and foreign studies about Thailand with no Thai author or affiliation.

"Work about Thailand" is therefore not restricted to author affiliation — the
system searches Thai-language work, work produced in Thailand, work studying a
Thai population or region, and foreign work about Thailand.

## 4a. Repository invariants (binding, verbatim from the design brief)

These eight lines are the invariants the repository must hold at all times —
they are not aspirational, they are what every architectural decision below
exists to enforce:

1. **No source → No cite.**
2. **No resolved identity → No verified cite.**
3. **No fetched evidence → No strong verification.**
4. **AI may propose; sources must assert.**
5. **Semantic similarity can retrieve, but cannot prove identity.**
6. **NOT_FOUND does not mean DOES_NOT_EXIST.**
7. **Every citation must be traceable back to a real external record.**
8. **AI must never become the source of bibliographic facts.**

### Output does not need to be padded

System output does not need to be numerous. If a user asks for 10 citations
but the system can genuinely verify only 4, the system returns 4 — it must
never fabricate or pad in the remaining 6 just to hit the requested count.

This is the key distinction between ThaiCite and a generic "AI finds
papers" tool: the goal is not to make the AI answer as much as possible, but
to make whatever it does output **traceable, genuinely sourced, and hard to
let slip out of the model's imagination.**

## 5. Source registry by capability

Each database has different capabilities: PubMed is biomedical-specialized,
PMC has partial full text, ThaiJO has Thai journals, TNRR has Thai research
outputs and projects, Crossref specializes in DOI metadata, and OpenAlex has a
large scholarly graph. Using a single database creates unnecessary blind spots.

### DataCite
Role: DOI for datasets, reports, repository outputs, software, and
non-traditional research outputs. Public REST API supports search/harvest with
no authentication; OAI-PMH is available for bulk harvesting.

### OpenAlex — Global Discovery Layer
Role: **discovery + citation expansion**, never the final authority for a
claim. Used for keyword search, full-text-aware search, semantic search,
related works, referenced works, cited-by expansion, author/institution
discovery. OpenAlex's own citation resolution favors shared PID/DOI first,
bibliographic matching only when no DOI exists — this system adopts the same
principle. Semantic search is for candidate discovery/ranking only, never for
confirming that a paper is the same paper or that it supports a claim.

### ThaiJO — Primary Thai journal metadata
Uses OAI-PMH directly; does not scrape HTML for metadata harvesting. ThaiJO's
public OAI-PMH exposes article title, authors, DOI, and publication date, with
separate endpoints per database group.

Adapter-level rate control:
```yaml
thaijo:
  max_requests_per_minute: 10
  throttled_requests_per_minute: 3
  incremental_sync: true
  use_resumption_token: true
```
ThaiJO throttles above 10 requests/minute/IP and may temporarily block beyond
that.

Role: Thai journal discovery — Thai title, English title where deposited,
authors, DOI, publication date, journal, landing URL, Thai/English abstract
where available.

### TNRR — National Research Repository
Covers Thai research output beyond journal articles. The adapter uses the
official API when a credential is available — TNRR's ResearchOutput and
Researcher APIs require authentication/token first. ResearchOutput search
supports title, author, co-author, institution, OECD field, and full-report
availability; records carry DOI, Thai/English abstract, a public link, and
full-report status.

```text
TNRRAdapter
 ├── authenticate
 ├── search_outputs
 ├── get_output
 ├── search_researchers
 └── fetch_public_report_if_allowed
```
Credentials must never be committed to git.

### TCI — Journal Quality / Citation Metadata Layer
Not a substitute for ThaiJO. Role: journal identity, TCI inclusion status,
TCI group/tier, journal metadata, and citation/reference enrichment where
officially accessible. TCI runs a Thai-journal quality evaluation system whose
current criteria reference article metadata (title, author, affiliation,
abstract, keywords, references) on the journal's own site — but there is no
confirmed public API for bulk article harvesting yet, so:

```text
TCI MUST NOT be aggressively scraped as core ingestion.
```

Preference order: official API/export → official downloadable data → human-
imported snapshot → official-page verification. No unofficial crawler may be
treated as canonical.

### TDC / ThaiLIS
Role: thesis, dissertation, research reports, Thai institutional academic
outputs. TDC describes itself as a full-text electronic repository for
theses, research reports, academic articles, and other document types.
Version 1 treats it as a **discovery adapter only**, until an official
bulk/API/OAI interface is confirmed. A search web page must never be assumed
to be an API.

## 6. Domain Router

The system must not hit every database on every query.

```text
GENERAL         → OpenAlex, Crossref, DataCite
THAI/THAILAND   → ThaiJO, TNRR, TDC, OpenAlex, Crossref
HEALTH/MEDICINE → PubMed, PMC, OpenAlex, Crossref
THAI HEALTH     → ThaiJO, TNRR, PubMed, PMC, OpenAlex, Crossref
DATASET         → DataCite, OpenAlex
PREPRINT/CS/PHYSICS → arXiv, OpenAlex, Crossref
```

Multi-route is allowed; the router is not restricted to one route per query.

## 7. PubMed Ecosystem

PubMed is a dedicated adapter, not generic web search.

```text
PubMedAdapter
ESearch → PMIDs → ESummary/EFetch → ELink related
                → ELink references/citations → ECitMatch (when a citation string exists)
```

NCBI E-Utilities' ESearch queries Entrez; ECitMatch is specifically for
citation-string matching against Entrez records.

Biomedical context flow:
```text
context → PubMed query generation → ESearch → seed papers
        → related papers → citation expansion → PMCID check
```

## 8. PMC Full-text Layer

If a PMCID exists, check PMC first:
```text
PMCID → PMC ID Converter → license/status → BioC / OAI-PMH / E-Utilities → full text
```
NCBI requires automated PMC retrieval to go through PMC Cloud, OAI-PMH,
E-Utilities, or BioC — not every article permits text mining/reuse.

```text
DO NOT scrape PMC article HTML in bulk.
```

## 9. Query Planner

Takes context and produces query families. Example — the context

> "ความเหลื่อมล้ำทางการศึกษาของเด็กในพื้นที่ชายแดนใต้"

becomes internally:
```yaml
concepts:
  topic:
    - ความเหลื่อมล้ำทางการศึกษา
    - educational inequality
    - educational disparity
  geography:
    - ชายแดนใต้
    - Southern Thailand
    - Deep South
    - Pattani
    - Yala
    - Narathiwat
  population:
    - children
    - students
    - youth
```
LLM use is appropriate here — but its output is only a **SEARCH PLAN**, never a
citation.

## 10. Candidate Provenance Rule

Every candidate must originate from an adapter result.

```yaml
candidate:
  candidate_id: CAN-...
  source_adapter: OPENALEX
  source_record_id: W123456
  retrieved_at: ...
  raw_record_hash: sha256:...
  identifiers:
    doi:
    pmid:
    pmcid:
  metadata:
    title:
    authors:
    year:
    source:
```
Missing `source_adapter` or `source_record_id` = schema violation. This is the
first wall of the hallucination firewall.

## 11. Identity Resolution

Trust order:
```text
1. DOI exact
2. PMID exact
3. PMCID exact
4. arXiv ID exact
5. official repository ID
6. ISSN + title + year + author
7. normalized title + author + year
8. semantic similarity — candidate ranking only, never merge authority
```
A single embedding score must never merge records on its own. Example: OpenAlex
W123 + Crossref DOI 10.x/x + PubMed PMID 123 + PMC PMC123 → ONE WORK. Keep the
identifier map:
```yaml
identifiers:
  doi:
  openalex:
  pmid:
  pmcid:
  thaijo_oai:
  tnrr_bibid:
```

## 12. Thai Normalization Layer

Needs a dedicated layer: พ.ศ.→ค.ศ., Thai digits→Arabic digits, Thai whitespace
normalization, zero-width character cleanup, name-prefix handling, Thai↔English
journal title, Thai↔English university name, Thai author name↔Romanized form,
DOI normalization, URL canonicalization. The `original_value` must always be
retained — never overwrite and discard the source value.

## 13. Deduplication

A record may arrive from ThaiJO, OpenAlex, Crossref, and PubMed simultaneously.
The system merges into a canonical work while every assertion still tracks its
provenance:
```yaml
canonical_work:
  title:
    canonical: "..."
    assertions:
      - value: "..."
        source: THAIJO
      - value: "..."
        source: CROSSREF
```
On conflict: **do not silently choose.** Set status `METADATA_CONFLICT` and
withhold the citation until a resolution rule handles it.

## 14. Fetch Policy

Three fetch tiers: `OFFICIAL_MACHINE_INTERFACE`, `OFFICIAL_PAGE`,
`DISCOVERY_WEB`. Preference order: official API/OAI → official
repository/publisher → DOI resolution → open-access repository → web
discovery. A search-result snippet is **never evidence**. A Google/DDG/Bing
result is a **candidate locator only**.

## 15. Full-text Acquisition

```text
candidate → OA location lookup → official repository/PMC/arXiv/ThaiJO
          → full text if legally/technically available → extract searchable text
```
Not every candidate needs a PDF download. Two stages: (A) title+abstract
screening, (B) full-text fetch only for top candidates — reduces both time and
load on upstream sources.

## 16. Context Match

Two layers: **LEXICAL MATCH** + **SEMANTIC MATCH**. Semantic score can reorder
candidates but cannot create a citation on its own.
```text
retrieval_score =
    lexical_relevance
  + semantic_relevance
  + Thailand_context_relevance
  + source_specific_signal
  + citation_neighbour_signal
```
This score is internal only, never exposed to the user.

> **Toledo reuse note (per this workspace's `EPIS-TOLEDO-FIRST` rule):** this
> is a weighted linear combination of scored signals — mathematically a dot
> product of a (currently all-1s) weight vector against a signal vector,
> expressed as a fold. Toledo already has this exact shape registered as
> **`A2/M.17.v1`** ("dot_is_fold", `Th_coqc`, current, `verdict.usable=true`),
> with `A2/M.10.v1` ("fold_linear") as a supporting parent. Cite `A2/M.17.v1`
> for the mathematical *form* of `retrieval_score`; only the specific weight
> values and signal definitions (not the linear-combination shape itself)
> would need their own registration if formalized further.

## 16a. Equation registry status for this project's other algorithmic formulas

Per `EPIS-TOLEDO-FIRST`/`EPIS-REUSE-PIPELINE`, every algorithmic formula in
this codebase was checked against Toledo (`~/ANSE.ASIA/toledo`) before being
treated as settled — reuse existing entries; register only the genuinely
missing piece, marked `PROPOSAL`, never cite an unregistered formula as if
final:

| Formula | Where used | Toledo status |
|---|---|---|
| `retrieval_score` weighted sum (§16 above) | Context Match ranking | **Reuse** — matches `A2/M.17.v1` ("dot_is_fold"), `Th_coqc`, current. Cite as-is. |
| Overlap coefficient — min-normalized set-intersection ratio (`\|A∩B\| / min(\|A\|,\|B\|)`) | `gate_g6_identity_match` (§17/§54) candidate-title-vs-query-citation token comparison | **NOT yet in Toledo** — searched and found no registered Jaccard/overlap/set-similarity equation at all; closest neighbour (`weld/M.66.v1`, a set-intersection identity, not a ratio) was too weak to parent it. Registered as a fresh `PROPOSAL` this session (not yet merged by a registrar) — carries the label **"not yet in Toledo"** until merged, per this workspace's fail-closed rule. A related but distinct signal in the same gate (`difflib.SequenceMatcher.ratio()`) is algorithm-specified (an LCS-like matching-block procedure), not a closed-form ratio, and was explicitly flagged in the proposal as needing its own, differently-worded entry if it is ever formalized — it must not be treated as the same equation as the overlap coefficient. |
| Thai-script density ratio (`count(Thai-script chars) / count(total chars)`) | `classify_thai_relevance` (§4a Thai-relevance tagging) THAI_LANGUAGE threshold | **Reuse-and-extend** — same shape as the already-registered `BiologyDomain_living_unit/B.03.v1` ("Population density = count/area", `Definition`, current), just applied to text/script classification instead of biology. Registered as a `PROPOSAL` parented to `B.03.v1` this session (not yet merged) — carries **"not yet in Toledo"** until merged. |

Both proposals were submitted via `toledo_register_proposal` on 2026-09-20 and
are pending human-registrar review (`mcp/proposals/` in the `toledo` repo,
`STATUS.jsonl` tracks them as `PENDING`). Until merged, the engine's own code
comments for `gate_g6_identity_match` and `classify_thai_relevance` should
likewise mark these two ratios "not yet in Toledo," per the fail-closed rule
in `EPIS-TOLEDO-FIRST`: no Toledo lookup/registration recorded ⇒ do not
present an equation as settled anywhere, including in this document.

## 17. Evidence Gate

Before a citation can become `VERIFIED`, it must pass:
```text
G1  source record exists
G2  identity resolved
G3  title/authors/year consistent enough
G4  official or accepted source URL resolves
G5  abstract or full text actually fetched
G6  content is relevant to supplied context
G7  no unresolved metadata conflict
```
For a strong claim that depends on paper content specifically:
```text
G8  supporting passage located
```
Failing G8 means the citation cannot receive strong-`VERIFIED` status.

## 18. Verification States

Internal only:
```text
DISCOVERED, IDENTIFIED, METADATA_VERIFIED, CONTENT_FETCHED, CONTEXT_MATCHED,
VERIFIED, CONFLICT, NOT_FOUND, FETCH_ERROR, REJECTED
```
The public API returns only `VERIFIED` results unless the user explicitly asks
for diagnostics.

## 19. NOT_FOUND Semantics

```text
NOT_FOUND ≠ DOES_NOT_EXIST
```
It means only: not found via the paths searched, at this time.
```yaml
not_found:
  adapters_checked:
  queries:
  checked_at:
  errors:
```
The system must never state "no research exists on this topic" merely because
nothing was found in the databases it searched.

## 20. Ranking

Ranking happens only after verification. Usable variables: context relevance,
directness, study relevance, Thailand relevance if requested, publication
recency where relevant, method relevance, source completeness,
citation-neighbour relevance, open full-text availability. Citation count,
journal tier, and impact factor must never be used as a proxy for a claim's
truth. TCI tier/quartile is metadata enrichment only.

## 21. Diversity Pass

Prevents a 10-result list from being one cluster of near-identical papers.
Checks source database, country, language, year, study type, journal, author
overlap — but never trades away accuracy for diversity:
```text
accuracy first, diversity second
```

## 22. Citation Formatter

After a work passes `VERIFIED`:
```text
Canonical metadata → CSL-style normalized record → APA7 / Vancouver / IEEE / Chicago
```
Default output format: APA 7. Internal shape:
```yaml
cite:
  authors:
  year:
  title:
  container_title:
  volume:
  issue:
  pages:
  doi:
  url:
```
Formatting must be deterministic — never "LLM writes the citation from memory."

## 23. Repository Layout (target, v-full)

```text
thai-cite-engine/
│
├── README.md
├── ARCHITECTURE.md
├── TRUST_MODEL.md
├── SOURCE_POLICY.md
├── FETCH_POLICY.md
├── SECURITY.md
│
├── pyproject.toml
├── requirements.lock
│
├── config/
│   ├── sources.yaml
│   ├── domain_routes.yaml
│   ├── trust_levels.yaml
│   ├── fetch_policy.yaml
│   └── ranking.yaml
│
├── schemas/
│   ├── request.schema.json
│   ├── candidate.schema.json
│   ├── work.schema.json
│   ├── provenance.schema.json
│   ├── evidence.schema.json
│   └── citation_output.schema.json
│
├── src/
│   └── thaicite/
│
│       ├── core/
│       │   ├── engine.py
│       │   ├── models.py
│       │   ├── states.py
│       │   └── exceptions.py
│       │
│       ├── routing/
│       │   ├── domain_router.py
│       │   └── query_planner.py
│       │
│       ├── adapters/
│       │   ├── base.py
│       │   ├── openalex.py
│       │   ├── crossref.py
│       │   ├── datacite.py
│       │   ├── thaijo_oai.py
│       │   ├── tnrr.py
│       │   ├── tci.py
│       │   ├── thailis.py
│       │   ├── pubmed.py
│       │   ├── pmc.py
│       │   └── arxiv.py
│       │
│       ├── normalize/
│       │   ├── thai_text.py
│       │   ├── names.py
│       │   ├── dates.py
│       │   ├── identifiers.py
│       │   └── journals.py
│       │
│       ├── resolve/
│       │   ├── identity.py
│       │   ├── deduplicate.py
│       │   └── conflicts.py
│       │
│       ├── fetch/
│       │   ├── fetcher.py
│       │   ├── pdf.py
│       │   ├── html.py
│       │   ├── bioc.py
│       │   └── oai.py
│       │
│       ├── evidence/
│       │   ├── abstract_match.py
│       │   ├── fulltext_match.py
│       │   ├── passage_locator.py
│       │   └── verifier.py
│       │
│       ├── rank/
│       │   ├── relevance.py
│       │   ├── thailand.py
│       │   └── diversify.py
│       │
│       ├── format/
│       │   ├── csl.py
│       │   └── apa7.py
│       │
│       ├── provenance/
│       │   ├── ledger.py
│       │   └── hashing.py
│       │
│       └── api/
│           ├── service.py
│           └── cli.py
│
├── cache/
│   └── .gitkeep
│
├── data/
│   ├── .gitignore
│   └── README.md
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── hallucination/
│   ├── fixtures/
│   └── golden/
│
├── scripts/
│   ├── sync_thaijo.py
│   ├── sync_openalex.py
│   ├── audit_sources.py
│   └── verify_release.py
│
└── .github/
    └── workflows/
        ├── tests.yml
        ├── source-health.yml
        └── hallucination-gate.yml
```

## 24. Git Is Not a Data Warehouse

Git holds: source code, schemas, policies, the source registry, tests, golden
fixtures, manual corrections, release manifests. Git does not hold: the entire
ThaiJO database, an OpenAlex dump, the PMC corpus, mass PDF collections, or API
caches. Early-stage runtime uses DuckDB + Parquet + a local object/cache
directory — enough for standalone development. At scale: PostgreSQL + object
storage + a search/vector index, without changing the public API.

## 25. Source Registry (`config/sources.yaml`)

The most important config file. Every adapter must declare which fields it has
authority to verify — so there is never a situation where "AI saw something
from OpenAlex and writes a canonical DOI" without real verification.

```yaml
CROSSREF:
  role: [metadata_registry, identifier_resolution]
  canonical_fields: [doi, deposited_title, deposited_authors]
  discovery: true
  verification: true

OPENALEX:
  role: [discovery, citation_graph, semantic_search]
  canonical_fields: []
  discovery: true
  verification: false

THAIJO:
  role: [thai_journal_repository, metadata]
  protocol: OAI_PMH
  discovery: true
  verification: true

PUBMED:
  role: [biomedical_index, biomedical_discovery]
  discovery: true
  verification: true
```

## 26. Hallucination Firewall — 7 layers

```text
1. AI cannot create source records
2. Every candidate has source_record_id
3. Every identifier is mechanically normalized
4. Persistent IDs resolved against an authoritative registry
5. Metadata conflict blocks release
6. Evidence must actually be fetched
7. Output serializer only reads VERIFIED records
```
Even if the Query Planner's LLM hallucinates a paper title, if no adapter has
a matching record: no candidate object → no canonical work → no VERIFIED state
→ that paper cannot appear in output.

## 27. LLM Boundary

LLM may be used for: context interpretation, query expansion, Thai-English
concept mapping, domain classification, semantic reranking, context relevance
assessment. LLM may **not**: create a DOI, PMID, title, author, year, journal;
change fetched metadata; set `VERIFIED` by itself; or format a citation from
memory.

## 28. Raw Data Immutability

Every source response is hashed:
```yaml
raw_record:
  source: CROSSREF
  fetched_at:
  request_url:
  sha256:
```
Normalized record stays separate from raw:
```text
RAW → NORMALIZED → RESOLVED
```
RAW is never edited. If upstream metadata changes, create a new snapshot.

## 29. Cache Strategy

```text
Crossref metadata     7–30 days
OpenAlex search        short TTL
ThaiJO OAI records      incremental
PubMed metadata         medium TTL
PMC full text            license-dependent
TNRR                     per API policy
```
DOI identity cache may be kept longer than search results.

## 30. Incremental ThaiJO Harvest

No need to re-harvest the whole system every time:
```text
bootstrap: harvest all metadata gradually
daily:     harvest from=<last_sync_date>
store:     OAI identifier, datestamp, metadata hash
```
Skip reprocessing when the hash is unchanged.

## 31. Citation Expansion

For a high-match seed paper: references, cited-by, related works, same-author
relevant works. Expanded works re-enter the pipeline at the **Candidate**
stage — they are not auto-VERIFIED just because they connect to a seed.

## 32. CLI

```bash
thaicite find --context "..." --max 10
```
Diagnostic mode for developers:
```bash
thaicite find --context "..." --debug
```
shows queries, adapters, candidate counts, rejections, conflicts, fetch
failures.

## 33. API

```text
POST /v1/citations
{"context": "...", "max_results": 10}
→ {"citations": [...]}
```
The public API does not expose raw AI reasoning chains or internal reasoning.

## 34. Tests

At least 5 groups: SOURCE (API/OAI parsing correctness), IDENTITY (DOI/PMID/
Thai-record dedup correctness), THAI (Thai text, พ.ศ., Thai names, Thai DOIs),
HALLUCINATION (fake paper names must return 0 results), GOLDEN RETRIEVAL (a
fixed context must retrieve known-relevant work in top-k).

The most important hallucination fixture:
```text
"Somchai et al. 2025 — Quantum Buddhism Therapy in Pattani —
Journal of Advanced Siamese Quantum Medicine — DOI 10.9999/fake.123"
```
Expected: NOT RESOLVED, NOT VERIFIED, NOT OUTPUT. Also test: an LLM generating
a plausible fake DOI must be killed immediately by the registry resolver.

## 35. Source Failure Tests

Must distinguish NOT_FOUND, RATE_LIMITED, TIMEOUT, ACCESS_DENIED,
PARSER_ERROR — never collapsed to one bucket. A Semantic Scholar/PubMed outage
does not mean the paper does not exist.

## 36. CI

Every PR: schema validation, unit tests, adapter fixture tests, hallucination
tests, citation-formatter tests, Thai normalization tests, no-secret scan.
Scheduled: source health check (endpoint reachability only, no bulk data pull
in CI).

## 37. Secrets

TNRR token/API account etc.: environment variables, GitHub Secrets, local
`.env`. `.env` is **never committed**.

## 38. Source Priority Overview

```text
IDENTITY/METADATA: Crossref, DataCite, PubMed, ThaiJO, TNRR, official repository
        ↓
DISCOVERY: OpenAlex, PubMed related, citation graph, TDC, other domain indexes
        ↓
WEB: official publisher/repository fetch (last resort, never a primary citation source)
```

## 39. v0.1 Source Set

Do not start with 15 adapters at once. Core first: **OpenAlex, Crossref,
ThaiJO, PubMed, PMC**. Then add **DataCite, TNRR** once the architecture is
stable. TCI/TDC arrive later as enrichment/discovery adapters.

## 40. v0.1 Acceptance Criteria

The system is usable when:
```text
Thai context           → finds real ThaiJO results
English context re: Thailand → OpenAlex finds foreign work
Health context          → PubMed route works
PMCID present            → PMC official full-text route works
DOI present               → Crossref resolves it
Same work, multiple sources → dedups to 1 citation
Fake paper                 → never leaks into output
API failure                  → not translated into NOT_FOUND
Metadata conflict             → never released as VERIFIED
Output                          → citation list only
```

## 41. Final System Definition

ThaiCite is not a new research database. It is:

```text
Federated scholarly citation resolver
+ Thailand-aware discovery
+ domain-specific routing
+ source-verification firewall
```

Shortest form:
```text
CONTEXT → UNDERSTAND → ROUTE → SEARCH REAL DATABASES → RESOLVE REAL RECORDS
        → FETCH REAL SOURCES → VERIFY → DEDUP → RANK → FORMAT → VERIFIED CITE LIST
```

The single most important invariant of the whole repository:

```text
AI NEVER BECOMES THE SOURCE.
```

AI may help search, translate context, expand terms, and rank — but every
citation that reaches output must be traceable back to a real record pulled
from a real external system, every time.

## 42. Engineering notes

- Modular adapter architecture: databases can be added/removed without
  changing the core pipeline. Each adapter must declare which of discovery,
  identity, metadata, full-text, or verification it serves — no adapter gets
  more authority than its source actually supports.
- "AI hallucinated a plausible paper" and "AI got the fabricated title wrong"
  are the same failure mode from this system's point of view — both are
  handled by the same firewall (no source record → no candidate), not by
  trying to detect hallucination directly.
- The end goal is not another research database — it is a **federated
  scholarly citation resolver that understands Thailand, has domain-specific
  routing, and has a source-verification firewall built in.**
- The design brief's own closing assessment (kept verbatim in spirit): this is
  considered a baseline ready to build a real repo from — specifically because
  separating **source adapter / identity resolution / evidence gate / output
  serializer** from each other means Thai-specific or domain-specific
  databases can be added later without tearing up the core system.

## 43. References cited in the design brief

These sources back specific claims made above (rate limits, protocol support,
API capabilities) — kept here so the architecture's own factual claims stay
checkable, consistent with the project's own "every claim must be traceable"
principle:

1. DataCite REST API — no-auth search/harvest, OAI-PMH support:
   https://support.datacite.org/docs/rest-api
2. OpenAlex API recipes (citation-graph walking via `referenced_works`,
   `cites`, `cited_by`, `related_works`):
   https://help.openalex.org/how-to/api-recipes/
3. OpenAlex — Citations (Works): https://help.openalex.org/data/works/citations/
4. OpenAlex — Semantic Search (candidate discovery/ranking only, not identity
   proof): https://help.openalex.org/api/semantic-search/
5. ThaiJO OAI Service (rate limits: 10 req/min/IP before throttling):
   https://www.tci-thaijo.org/public/oai.html
6. Thai Journal Citation Index Centre (TCI) evaluation round 5 criteria:
   https://tci-thailand.org/backend/download/Evaluation_5/EvaluationRound5.pdf
7. ThaiLIS/TDC — full-text electronic thesis/research-report repository:
   https://tdc.thailis.or.th/tdc/browse.php
8. NCBI E-Utilities In-Depth (ESearch, ECitMatch):
   https://www.ncbi.nlm.nih.gov/books/NBK25499/
9. PMC — For Developers (PMC Cloud, OAI-PMH, E-Utilities, BioC; not all
   articles permit automated text mining): https://pmc.ncbi.nlm.nih.gov/tools/developers/
10. Crossref REST API (metadata is publisher/member-deposited; has a
    report-bad-metadata channel): https://www.crossref.org/documentation/retrieve-metadata/rest-api/
11. Thai-Journal Online (ThaiJO) homepage: https://www.tci-thaijo.org/th
12. OpenAlex — Works Overview: https://help.openalex.org/data/works/
13. ThaiJO downloads page (bulk-metadata option, referenced re: "do not mirror
    420k+ records into a local DB on day one"): https://www.tci-thaijo.org/pages/download

---

# PART II — Design Evolution (supersedes SCOPE, not the core invariants above)

> **Relationship to Part I:** Part I (§0–43) is the long-term, full-scope
> target architecture as originally briefed. Part II below records how the
> design was deliberately narrowed after further review — it changes *what
> gets built first* and *how identity/verification work internally*, but it
> does not weaken any invariant in §4a; if anything it adds a stronger one
> (claim–citation / scope verification) that Part I's evidence gate (G1–G8)
> only partially covered. Where Part II and Part I disagree on scope or
> deployment shape, **Part II is the current direction** for v1. Part I stays
> as the reference for what the full system grows into later (TNRR full
> sync, TCITE public ID infrastructure, TCI/TDC integration, etc.).
>
> Note for whoever reads the concept-validation run (`docs/HANDOFF_2026-09-20.md`,
> run `wf_3b5765ac-594`): that prototype was scaffolded against Part I's
> `resolve_citations()` / single-`VERIFIED`-state framing, before Part II's
> Admission Gate / P-A-M framing existed. The core firewall invariant it
> tests (no source record → no candidate → nothing fabricated ships) is still
> valid and still the right thing to validate first. Reconciling the
> prototype's shape with the Admission Gate (§53–55) and evidence levels
> P/A/M (§55) is follow-up work, not a defect in the validation run itself.

## 43a. Terminology reconciliation — Part I ↔ Part II

Both parts describe the same firewall; Part II just renames and shrinks some
of Part I's machinery for v1. Reading this table top-to-bottom should remove
any sense that the two parts disagree:

| Part I (§0–43, full target) | Part II (§44–65, v1 direction) | Reconciliation |
|---|---|---|
| Single `VERIFIED` state (§18) | Evidence levels `P`/`A`/`M`, only `P`/`A` may output (§55) | Same output rule ("don't ship unless the content actually backs it"); Part II just makes the resolution of *how* it was verified explicit instead of collapsing it into one label. Anything Part I would call `VERIFIED` is a `P`- or `A`-level `ADMITTED` result in Part II. |
| Evidence Gate G1–G7, plus G8 for strong claims (§17) | Admission Gate's 3 checks: Identity, Content, Scope (§54) | G1–G4 (source exists, identity resolved, metadata consistent, URL resolves) ≈ the **Identity** check. G5 (content fetched) ≈ **Content**. G6/G7/G8 (context relevance, no conflict, supporting passage) ≈ **Scope**. Nothing was dropped — it was regrouped into 3 checks instead of 8 gates. |
| 9-state machine: `DISCOVERED…REJECTED` (§18) | 5-step chain `FOUND→IDENTITY_OK→CONTENT_SEEN→SCOPE_MATCHED→ADMITTED`, else `HOLD` (§53) | Part II's chain is the v1 build target; `NOT_FOUND`/`RATE_LIMITED`/`TIMEOUT`/`ACCESS_DENIED`/`PARSER_ERROR` (§18–19) still apply *before* `FOUND` and are not replaced. `METADATA_CONFLICT`/`CONFLICT` (§13, §18) still applies and blocks `ADMITTED` the same way it blocked `VERIFIED`. |
| `Candidate` schema, `source_adapter`+`source_record_id` required (§10) | `Evidence Capsule` (§58) | Same required-provenance rule, lighter shape. `Evidence Capsule.source_id` plays the role of `source_adapter`+`source_record_id` combined. |
| `TCITE-W-*` canonical Work ID + alias/redirect model (§44) | Paused for v1 — use source-native keys (`DOI:`, `PMID:`, `THAIJO:`, `OPENALEX:`) directly (§59) | §44's design is not wrong, just not yet justified — build it once real duplicate-output evidence accumulates, per §59. |
| Candidate provenance rule numbering `CAN-...` (§10) | Not used in v1 — `source_id` in the Evidence Capsule is the only handle | Superseded by the simpler Evidence Capsule shape; no behavior lost. |
| 11-adapter full set incl. DataCite/TCI/TDC/TNRR/arXiv (§5, §39) | 4-core adapters: OpenAlex, Crossref, ThaiJO OAI, PubMed+PMC (§56) | §39's "core first" list (OpenAlex/Crossref/ThaiJO/PubMed/PMC) and §56's 4-row table describe the *same* v1 set (PubMed+PMC counted as one row in §56). TNRR is "adapter #5" in both (§39, §56). DataCite/TCI/TDC/arXiv/Scopus/WoS are deferred in both. |
| Resident daemon `thaicite-server`/`thaicited` always running (§45) | No resident daemon — Python package + stdio MCP, process starts per-session (§60) | §60 explicitly revises §45's daemon suggestion for v1. §45's "local vs. server mode from one core" framing still holds; server/daemon mode is a later deployment option once local-mode + stdio MCP is proven, not a day-one requirement. |
| Cache: DuckDB + Parquet + local object/cache dir (§24) | Cache: single SQLite file at `~/.cache/thaicite/cache.sqlite` (§61) | §61 supersedes §24's cache tech choice for v1 — SQLite is simpler and sufficient at v1 scale; DuckDB+Parquet remains the plan for when the system scales past v1 (§24's own "at scale: PostgreSQL + object storage" note is the tier *after* that). |
| Full repository layout, 11 adapters, `schemas/`, `config/`, `.github/workflows/` (§23) | Minimal repo layout: `engine.py`, `router.py`, `gate.py`, 4 adapters, `mcp/server.py`, `cli.py` (§61) | §61 is the actual v1 build target; §23 remains the reference for what the repo grows into once v1's benchmark (§62) justifies expansion. |
| Baseline pipeline: Context→Query planning→Search→…→Verified citation list (§3) | Final pipeline: Context→Search→Real source→readable evidence?→Cite/reject/HOLD (§64) | Same shape, §64 makes the three-way outcome (admit / reject / cannot-determine) explicit instead of leaving it implicit in "Verification gates." |
| 4a's 8 invariants | Unchanged, still binding | Part II adds emphasis (esp. invariant 6, `NOT_FOUND` ≠ `DOES_NOT_EXIST`, reappears as `0 ≠ ⊥` in §51) but does not relax or replace any of the 8. |

**Rule for resolving any future ambiguity not covered above:** if Part I and
Part II ever appear to conflict on *scope or deployment shape*, Part II wins
for v1. If they ever appear to conflict on an *invariant* (§4a) — that should
not be possible; if it looks like it is, treat it as a documentation bug and
fix the wording, not the invariant.

## 44. Work identity model — Source ID + Canonical Work ID (kept for later; see §59 for v1 status)

Must separate two different kinds of ID: the **ID of a record at its source**
versus the **ID of one work inside our own system** — because a single paper
can appear in PubMed, Crossref, OpenAlex, and ThaiJO simultaneously.

The strongest approach is **source-derived ID + canonical Work ID** — never an
ID generated from a title or invented by AI.

```text
ThaiJO record   → THAIJO:oai:tci-thaijo.org:123456
PubMed record   → PMID:38912345
Crossref record → DOI:10.1234/abcd.2025.001
OpenAlex record → OPENALEX:W4387654321
TNRR record     → TNRR:<native-record-id>
```

These may all be the same underlying work, so there is a canonical layer above
them:

```text
TCITE-W-8K4M2P7Q
 ├─ DOI:10.1234/abcd.2025.001
 ├─ PMID:38912345
 ├─ PMCID:PMC1234567
 ├─ OPENALEX:W4387654321
 └─ THAIJO:oai:...
```

Critically, `TCITE-W-8K4M2P7Q` is **never assigned by the AI** — it only
exists because a resolved source record produced it.

### Deterministic construction

Build the ID deterministically from `namespace + normalized_native_identifier`:

```text
sha256("DOI|10.1234/abcd.2025.001")   → TCITE-W-8K4M2P7Q
sha256("PMID|38912345")                → TCITE-W-X7D91K3A   (no DOI case)
sha256("THAIJO|oai:tci-thaijo.org:123456") → TCITE-W-Q3M8N1CZ (ThaiJO-only case)
```

### Immutability rule — never re-ID a work when a better source appears later

Example: today the work is found via ThaiJO first:
```text
TCITE-W-Q3M8N1CZ → THAIJO:123456
```
A month later a DOI turns up:
```text
TCITE-W-Q3M8N1CZ → THAIJO:123456 → DOI:10.xxxx/yyy → OPENALEX:W...
```
The ID `TCITE-W-Q3M8N1CZ` **does not change** just because a DOI appeared
later — otherwise every citation/bookmark/cache built against the old ID
breaks. This mirrors PubMed's own PMID behavior conceptually: a PMID is the
identifier of a record in PubMed, and it stays even as metadata grows; a new
number is never minted just because a DOI became known afterward.

### Three-layer identifier model

```text
1. SOURCE ID    — PMID:..., DOI:..., THAIJO:..., TNRR:..., OPENALEX:...
2. TCITE WORK ID — TCITE-W-Q3M8N1CZ = one work inside our system
3. ALIASES       — every identifier that points at the same work
```

Schema:
```yaml
work_id: TCITE-W-Q3M8N1CZ

created_from:
  namespace: THAIJO
  native_id: oai:tci-thaijo.org:123456

identifiers:
  thaijo: [oai:tci-thaijo.org:123456]
  doi: [10.1234/example.2025.01]
  pmid: ["38912345"]
  pmcid: [PMC1234567]
  openalex: [W4387654321]

identity_status: VERIFIED
```

This is a strong extra hallucination guard: AI cannot say "this work is
TCITE-W-XYZ" and have the system believe it, because a `TCITE-W-*` ID can only
be **minted from a real source-native record that already passed through an
adapter**:

```text
No real ThaiJO/PMID/DOI/TNRR/etc. record
              ↓
   Cannot construct a TCITE ID
              ↓
   Citation cannot enter the verified list
```

### Do not use running numbers

Avoid a PMID-style running counter (`TCITE:000000001`, `TCITE:000000002`) —
that requires a central authority handing out numbers and guarding against
collisions. A deterministic hash-based ID fits a standalone/open-source
architecture better: any machine ingesting the same source record computes
the same ID without calling a central server.

### Merge rule when two sources are ingested independently first

A paper ingested first from two different sources may briefly get two
different IDs:
```text
ThaiJO   → TCITE-W-AAAA
Crossref → TCITE-W-BBBB
```
When the resolver later finds DOI/title/author match (`AAAA == BBBB`), the
system must pick one as canonical and keep the other as a **permanent alias**:
```text
TCITE-W-AAAA → canonical
TCITE-W-BBBB → redirects_to: TCITE-W-AAAA
```
**Old IDs are never deleted.** This gives the system properties similar to
real scholarly-identifier infrastructure.

### Citation output shape (if/when TCITE ID ships)

```yaml
- tcite_id: TCITE-W-Q3M8N1CZ
  title: ...
  authors: ...
  year: 2025
  doi: 10.xxxx/yyy
  pmid: 38912345
  source_url: ...
  citation: ...
```
This is preferred over returning a bare DOI, because the TCITE ID becomes the
unification point for a work across ecosystems — especially valuable for Thai
work that has neither a DOI nor a PMID, which is exactly where international
systems have the largest gap.

Summary:
> Source creates the right for an ID to exist — not AI.
> One source record has its own source ID.
> Many source IDs can merge into one TCITE Work ID.
> A TCITE ID, once created, must be immutable, and redirectable on merge.

## 45. Deployment architecture — Engine, not Skill

If the intent is the same pattern as **mimi-remote** (a runtime resident on
the machine that an AI calls from a session), ThaiCite should be
designed that way directly, rather than as a Skill alone.

```text
GitHub Repository
      ↓ install once
ThaiCite
      ↓
Local Engine / Service
      ├── CLI
      ├── API
      └── MCP / Tool interface
              ↓
      ChatGPT / Codex / other AI agents
              ↓
          called from a session
```

**The Git repo is not a Skill.** It is an **Engine/Tool**. A Skill is only "a
manual telling AI when and how to call this Tool." A Skill-only
implementation has a real problem: skills are mostly instructions/Markdown
with no database, cache, HTTP clients, deduplication, PubMed/ThaiJO adapters,
or verification engine of their own.

Three clear layers:
```text
1. CORE ENGINE
   thaicite Python package
   ├── adapters
   ├── resolver
   ├── verifier
   ├── citation formatter
   └── local database/cache

2. RUNTIME
   thaicite-server / thaicited — running on the machine
   ↓ receives: find_citations(context)

3. AI INTERFACE
   MCP / plugin / skill
   ↓ lets AI call the engine
```

Install once (conceptually):
```bash
git clone ...
cd thai-cite-engine
pip install -e .
thaicite setup
```
Then either a one-shot command:
```bash
thaicite find "context to find citations for"
```
or a running service that an AI session calls directly:
```bash
thaicited start
```
```text
find_citations(context="...")
```

### Mimi-pattern alignment

mimi-remote's approach (per its README) is a resident/local runtime plus a
standalone Skill that helps AI install/manage that runtime — the skill is not
the engine itself, it's an interface/installer wrapped around the engine.
ThaiCite can use the same pattern:
```text
GitHub
├── Engine
├── Local daemon (thaicited)
├── MCP server
├── CLI
└── skills/thai-cite/SKILL.md
```
The Skill then reads to an AI roughly as:
```text
When the user wants to find a citation
→ call thai_cite.find
→ do not create the citation yourself
→ use only the engine's output
```
This is strong anti-hallucination framing precisely because the Skill never
tells the AI "try thinking of a citation" — it says: **when this is a
citation-finding task, send context to the engine and take back a verified
cite.**

### Narrow public interface — one tool, not forty

The engine should not expose 40 API calls for the AI to pick from freely.
Public interface, primarily one call:
```text
thai_cite.find(context) → citation list
```
Possibly a few more for specific tasks:
```text
thai_cite.resolve(identifier)
thai_cite.verify(citation)
thai_cite.health()
```
but the main command is `find(context) → citation list`. Internally it
decides for itself whether to use OpenAlex, Crossref, ThaiJO, TNRR, PubMed,
PMC, or DataCite — the AI never needs to know.

### Local vs. server mode from one core

It can also be installed as a **remote service**:
```text
AI session → HTTPS → api.thaicite... → ThaiCite
```
Advantage: every machine can call it immediately without installing a local
database everywhere. The architecture should support both modes from the same
core:
```text
               Thai Cite Core
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
      Local Mode            Server Mode
      localhost             HTTPS API
          │                     │
          └──────────┬──────────┘
                     ▼
                  MCP Tool
                     ▼
                    AI
```
**Local-first for the development phase** — easier to debug, no server cost,
TNRR/API credentials can stay on the machine, and it can be tried against
any AI coding assistant immediately. Once stable, deploy a server without
changing the core.

So: **ThaiCite is a standalone scholarly citation tool/service, not a
Skill** — but it ships with a **Skill + MCP wrapper** so AI can call it from a
session like a built-in capability:
```text
Git Repo → install once → ThaiCite running locally
         → MCP/Tool registered → any AI session can call it
         → Context → Thai Cite → Cite list
```
This pattern is preferred over having AI `git clone` and re-run a script every
session — the goal is for it to feel like "the machine has a citation-finding
system installed," not "there is a repository the AI can go read."

> **Revised for v1 in §60:** the resident-daemon suggestion above
> (`thaicite-server`/`thaicited` running continuously) is replaced for v1 by a
> Python package invoked over stdio MCP, started per-session rather than kept
> running. The local-vs-server-mode framing and the "one narrow tool"
> interface above still hold; only the always-on-daemon part changes.

## 46. The hallucination this architecture alone does not fully solve

The strongest thing about this design is that it does not try to make the AI
"better at remembering papers" — it restructures the flow from

> AI → thinks of a paper name → writes a citation

into

> AI → builds a search plan → real database → real record → resolve identity
> → fetch content → verify → cite

**Problem 1 solved for real: fabricated papers.** If a citation object can
only originate from an adapter result, the AI cannot invent a title, DOI, or
author and ship it. If a model says "Smith et al. (2024), AI and Rural
Education in Thailand, doi:10.1234/fake" but Crossref/PubMed/ThaiJO/TNRR never
produce that source record, there is no object that can pass the output
serializer. This is a **software constraint**, which is stronger than a
prompt asking the model "please don't hallucinate." Crossref fits this role
well because most of its metadata is publisher-deposited directly, and its API
is open for DOI/metadata checks; OpenAlex itself treats a shared PID as the
strongest path to merge records, falling back to fuzzy matching only when no
PID exists.

**Problem 2 solved for real: fragmented databases**, especially for Thailand.
ThaiJO currently holds 400,000+ articles with an OAI interface; TNRR (วช.)
separately holds research projects, outputs, researchers, and theses, not just
journal articles. A single layer that queries these and deduplicates has clear
value.

**Problem 3 solved for real: domain-specific databases.** Using OpenAlex alone
loses the benefit of the PubMed ecosystem's own query syntax and tools such as
ESearch/ECitMatch — so Domain Router has real justification, not complexity
added for its own sake.

**Problem 4 solved for real: one paper appearing in many databases** (e.g.
PubMed + Crossref + OpenAlex + PMC) — resolving to one work and keeping
PMID/DOI/OpenAlex ID as aliases meaningfully reduces duplicate citations;
OpenAlex itself uses the same PID-first-then-fuzzy-match principle.

### But there is a second hallucination class this alone does NOT fix

Suppose a paper is completely real:
```text
Real DOI ✓  Real title ✓  Real authors ✓  Real PubMed record ✓
```
but it gets cited as supporting

> "Using AI improves students' critical thinking"

when the real paper actually studied "AI made students complete assignments
faster," not critical thinking. This is **claim–citation hallucination**, and
it is more dangerous than a fake paper name, because the citation *looks*
completely legitimate and a human reviewer may not catch it.

So if ThaiCite stops at:
```text
Real paper found → VERIFIED
```
that is **not enough**. It should be:
```text
Real paper found
        ↓
IDENTITY VERIFIED
        ↓
abstract/full text fetched
        ↓
is the content actually about this context?
        ↓
for a specific claim: is a supporting passage actually found?
        ↓
VERIFIED CITE
```
This single addition substantially increases the system's real value.

## 47. Honest weaknesses (acknowledged up front — 7 items)

1. **Cannot guarantee finding every relevant work in the world.** OpenAlex is
   huge and has a citation graph, but OpenAlex itself describes citation
   construction as extracting references and matching them back to works;
   DOI matching is more trustworthy than bibliographic matching, and some
   matches will inevitably be missed. The system should claim "best available
   retrieval," never "the complete literature universe."
2. **Real metadata is not always correct metadata.** Crossref data is
   publisher/member-deposited, which is trustworthy for provenance but can
   still be missing, wrong, or incomplete — Crossref itself has a channel for
   reporting bad/incomplete metadata. Multi-source reconciliation remains
   necessary.
3. **Older Thai work often lacks a DOI**, making identity resolution harder —
   falling back to title+author+year+journal, with real remaining ambiguity.
   Creating a TCITE ID does not make this ambiguity disappear; it only helps
   manage it.
4. **Full text is not open for every work.** Metadata can be checked, but
   some papers sit behind a paywall — such records may reach
   `METADATA_VERIFIED` but must not be claimed as claim-match verified.
5. **Thai-language semantic retrieval is a real risk area.** Thai/English are
   not always literal translations of each other, especially in social
   science, legal, religious, education, or locally-contextual terminology —
   LLM/query expansion helps but introduces both false positives and false
   negatives.
6. **An own-ID system carries hidden cost.** A TCITE ID is useful for
   dedup/provenance, but the moment records merge/split, the project becomes
   the steward of identifier infrastructure — needing redirects, tombstones,
   merge history, and a rule against ID reuse. Done carelessly, this creates
   more problems than it solves.
7. **The full system as previously scoped is too large for an MVP.** This is
   the point that most needed correction from the earlier architecture: there
   is no need to build a large local database, a nationwide Thai citation
   graph, TCI integration, full TNRR sync, and a TCITE resolver all at once,
   before proving retrieval quality.

## 48. First cut-down: reduced Version 1 pipeline

To prove real value first:
```text
Context
   ↓
Query Planner TH/EN
   ↓
Domain Router
   ↓
OpenAlex + Crossref
ThaiJO
PubMed/PMC when relevant
   ↓
Identity Resolver
   ↓
Deduplicate
   ↓
Abstract/full-text fetch
   ↓
Context/passage verification
   ↓
Citation list
```
**This alone is enough to test the product's core hypothesis.** TNRR, TDC,
DataCite, TCI, an own index, and TCITE permanent IDs are added only after a
benchmark shows V1 genuinely outperforms using OpenAlex/PubMed/Google Scholar
directly. Do not even start by downloading ThaiJO's 400,000+ records into a
local database — ThaiJO already exposes a search/harvest interface.

## 49. Who this is for, and who it is not for

**Strong fit:** researchers, students, faculty, academic writers, people doing
literature reviews, and AI coding/research agents that must work in both Thai
and English — especially Thailand-related work, since the data sits across
both Thai and international ecosystems.

**Also strong fit:** an organization that wants an AI writer forbidden from
inventing its own citations. ThaiCite can become a **mandatory
citation gateway**:
```text
AI Writer → needs a citation → ThaiCite → Verified citation
```
If the engine returns nothing, the AI has no right to cite anything. This is
a strong use case.

**Not a great fit** for someone who just wants to type a paper's name and find
its DOI — Crossref/OpenAlex/PubMed already do that directly; this system would
be heavier than necessary.

**Not a good fit** for a systematic review that must certify "searched
completely per protocol," while the system does not yet cover subscription
databases such as Scopus/Web of Science/Embase/CINAHL/PsycINFO depending on
field. In that case ThaiCite helps with discovery/verification but
should not claim to substitute for a systematic-review search protocol.

**Not a good fit** for a user wanting a ready-made academic conclusion,
because this system should only answer a narrow question:

> "What is a relevant, checkable citation?"

never

> "What is the correct academic conclusion?"

## 50. Reframed core value — a Citation Firewall between AI and scholarship

The project started from "find better Thai citations," but after laying out
the architecture, the stronger value is:

> **It is a Citation Firewall between AI and scholarship.**

AI may propose anything internally, but a citation cannot leave the system
until a real external record supports it. Adding **passage verification** as
another layer guards both levels:
```text
Citation hallucination — "the paper doesn't exist"
        ↓ fixed by identity/source verification

Citation misuse — "the paper exists but doesn't say what we're claiming"
        ↓ fixed by content/passage verification
```
Given this, the plan is to **cut early infrastructure ambition by roughly
half** and invest more in benchmark and verification than ingestion — because
success is not measured by "we have 20 million records," it is measured by:

> Out of 100–500 real contexts, what percentage of the time does the system
> find citations that are the right work, genuinely relevant, never
> fabricated, and never claiming more than the source actually supports?

If this benchmark beats the baseline, the system has earned the case for full
development.

## 51. Final reframing (after reading `main.hub`) — Cite Admission Engine, not a research graph

Re-reading `main.hub`'s Step 0 changes the framing again:

> We do not have a "missing research database" problem.
> We have the problem that **AI turns context into a citation with no point
> that proves that citation has the right to be exported.**

So the next system to build is not a Research Graph, a TCITE Registry, a
vector database, or a nationwide ThaiJO mirror — it is a **Cite Admission
Engine**: the gate that decides whether one candidate "gets to become a
Cite yet."

This follows `main.hub`'s Step 0 directly: read input as a finite readout
before naming the problem, and never let a claim's strength exceed what was
actually read (`main.hub/ROUTES.md`, `readout-universe` SKILL.md).

### Lens note

Raw input, roughly: "give context, find genuinely relevant, accurate
citations, both Thai and international, without hallucinating." Through this
workspace's own lens, what must be preserved is not "the paper's name" but
this distinction:

```text
candidate source   ≠   source with the right to be used as a cite
```

Agency = the ThaiCite system. Query = the context the user gives. Access
relation = the API/OAI/full text actually reachable. Resolution = the
metadata + abstract/passage the system can actually read. What must be judged:

```text
Is the evidence we can read, at this resolution,
enough to change status from CANDIDATE → ADMISSIBLE CITE?
```

And this distinction must be preserved:
```text
0 ≠ ⊥
```
i.e. `NO_MATCH` is not the same as `CANNOT_VERIFY` — matching `skillme`'s own
requirement to separate metadata verification from scope verification, and to
never write "local evidence was not found" as "local evidence does not
exist" (`skillme` SKILL.md).

## 52. Minimal v1 system diagram (current build target)

```text
                  CONTEXT
                     │
                     ▼
               QUERY PLANNER
              Thai + English
                     │
                     ▼
               SOURCE ROUTER
                     │
       ┌─────────────┼──────────────┐
       ▼             ▼              ▼
   OpenAlex       ThaiJO       PubMed/PMC
       │             │              │
       └─────── Crossref ───────────┘
                     │
                     ▼
                CANDIDATES
                     │
                     ▼
           CITE ADMISSION GATE
                     │
             ┌───────┴───────┐
             ▼               ▼
           ADMIT             HOLD
             │
             ▼
          CITE LIST
```

**This much, first.** Explicitly not (for v1): building our own research
graph, storing all research, mirroring ThaiJO, a vector database, public
TCITE ID infrastructure from day one, or an always-on daemon.

## 53. Cite Admission Gate — state machine

A small state machine:
```text
FOUND → IDENTITY_OK → CONTENT_SEEN → SCOPE_MATCHED → ADMITTED
```
Whenever it cannot proceed at any step: `HOLD`.

This is the DLEH principle applied as directly as possible:

> **Fail closed for truth; remain open for imagination.**

DLEH separates, very clearly, how freely a model may generate a candidate
(as wide as it wants) from the fact that a candidate has no authority to be
*authorized* until it leaves the imagination lane and passes evidence +
verification (`dual-lane-epistemic-harness` README).

In ThaiCite, the same split applies:
```text
LLM: "there's probably a paper like this"  → DISCOVERY (no problem)
LLM: "you can cite this paper"             → NO AUTHORITY (belongs to the gate)
```

## 54. Admission Gate — exactly 3 checks

This is what keeps the system light.

**Identity — does this work genuinely exist, and do we know which work it
is?** e.g. DOI / PMID / PMCID / ThaiJO OAI ID / OpenAlex ID:
```text
DOI resolves?
PMID resolves?
native record exists?
metadata consistent?
```
Passing this does not mean it can be cited yet — it only means the paper has
an identity.

**Content — have we actually read the source's text?** Order:
```text
full text
   ↓ if unavailable
abstract
   ↓ if unavailable
metadata only
```
`metadata only` can never pass scope verification.

**Scope — does what we actually read support the context the user asked
about?** Example:

Context: "AI improves critical thinking."
Paper: "Students completed assignments faster with AI."

The paper is real, the metadata is correct, but:
```text
scope_match = false
```
so:
```text
HOLD / REJECT
```
This is exactly the failure mode a DOI-only hallucination firewall cannot
catch.

## 55. Evidence levels — P / A / M (replaces a single "VERIFIED" label)

Internally, only three evidence levels:
```text
P — PASSAGE:  a matching passage was found in the full text
A — ABSTRACT: the abstract directly supports the context
M — METADATA: the work is known to be real, but not yet known to support
               the context
```
Default output policy:
```text
P and A → may be output
M       → must never be output
```
This matches this workspace's own `readout-universe` discipline better than a
single blanket `VERIFIED` label, since each level was checked at a different
resolution.

## 56. Reduced Source Router — 4 core adapters for v1

| Adapter | Role |
|---|---|
| OpenAlex | World candidate discovery + citation neighborhood |
| Crossref | DOI/metadata confirmation |
| ThaiJO OAI | Thai journal work |
| PubMed + PMC | Biomedical path + full text |

**TNRR** becomes the 5th adapter once credentials/API access are ready. TCI,
TDC, DataCite, Semantic Scholar, arXiv, Scopus, and Web of Science are
explicitly **not** in the first core — not because they lack value, but
because they don't yet move the system's core retained difference enough to
justify the added surface.

## 57. AI boundary, restated under the Admission Gate framing

```text
AI CAN:
  context → concepts
  Thai ↔ English terms
  domain detection
  query expansion
  candidate reranking
  scope assessment (as a suggestion, still checked by the gate)
```
```text
LLM output
    ├── query terms       ✓
    ├── ranking           ✓
    ├── scope suggestion  ✓
    └── citation record   ✗
```
A citation record must come from an adapter only — this is the single most
important wall in the whole system.

## 58. Evidence Capsule (v1 replacement for a heavier canonical-work object)

No knowledge graph needed — one small object per candidate is enough:
```yaml
source_id: PMID:12345678

metadata:
  title: ...
  authors: ...
  year: ...
  doi: ...

retrieved_from:
  source: pubmed
  fetched_at: ...

evidence:
  level: A
  text: "..."
  locator: abstract

scope:
  relation: SUPPORTS
  status: MATCHED

decision: ADMITTED
```
This is enough to answer, after the fact, "why did this cite come out" —
without needing a nationwide research graph.

## 59. TCITE Work ID — paused for v1 (design from §44 kept for later)

Through this workspace's lens, the current problem is not

> "one work lacks our own identifier"

it is

> "AI can propose a cite without sufficient evidence."

So the TCITE ID does not change this system's core retained difference for
v1. Version 1 uses source-native keys directly — `DOI:...`, `PMID:...`,
`THAIJO:...`, `OPENALEX:...` — with no internal canonical-ID layer yet. Build
a real internal canonical Work ID only once there is real evidence that
ThaiJO + Crossref + PubMed records for the same work are being duplicated in
output often enough to matter. No public identifier infrastructure on day
one — this cuts a large amount of unnecessary work.

## 60. Deployment for v1 — no resident daemon; Python package + MCP stdio

Revising §45's earlier suggestion: **v1 does not need a Mimi-style resident
daemon.** Instead:
```text
install once
     ↓
MCP client calls it
     ↓
process starts only when a session needs it
     ↓
closes when done
```
e.g. `thai_cite.find(context, limit=10)` — a single tool; the router handles
everything internally. Without MCP, fall back to:
```bash
thaicite find "context..."
```
`main.hub/SURFACES.md` itself treats Skill, MCP, CLI, and API as separate
surfaces and does not require an always-on service — stdio MCP + CLI fits
this workspace's ecosystem and is much lighter than a daemon.

## 61. Minimal on-disk layout for v1 (build target — §23 stays the long-term full-system reference)

```text
thaicite/
│
├── README.md
├── TRUST.md
│
├── src/thaicite/
│   ├── engine.py
│   ├── router.py
│   ├── gate.py
│   ├── models.py
│   │
│   ├── adapters/
│   │   ├── openalex.py
│   │   ├── crossref.py
│   │   ├── thaijo.py
│   │   └── pubmed.py
│   │
│   ├── fetch.py
│   └── format.py
│
├── mcp/
│   └── server.py
│
├── cli.py
│
└── tests/
    ├── fake_citation.py
    ├── scope_mismatch.py
    ├── api_failure.py
    └── known_cases.py
```
Cache: a single SQLite file at `~/.cache/thaicite/cache.sqlite`. No
PostgreSQL, no Elasticsearch, no Qdrant, no Neo4j for v1.

## 62. Resource allocation — invest in benchmark, not infrastructure

This is a deliberate weight shift from the earlier architecture. Out of 100
units of effort, do not spend 70 building databases. Roughly:
```text
30  adapters + admission gate
50  benchmark
20  MCP / packaging / tests
```
What must actually be proven:
```text
100–500 real contexts
        ↓
what citations does the system return?
        ↓
human review:
  does the work genuinely exist?
  is it genuinely relevant?
  does it overclaim?
  did an important citation get missed?
  does it find Thai work when it should?
```
`main.hub/GOAL.md` states this principle well: if Step 0 doesn't change real
behavior, it's ceremony, and should be cut or fixed rather than kept because
it looks good. The same principle applies here:

> If a component does not improve precision, recall, or citation-scope
> accuracy on the benchmark — cut it.

## 63. The only three things that genuinely need building for v1

1. **Source Router** — routes context to the sources that should be searched.
2. **Evidence Capsule** — records what was actually, genuinely read from a
   candidate.
3. **Cite Admission Gate** — decides `ADMIT` / `HOLD`, fail-closed.

Query Planner, the formatter, and the MCP wrapper are thin wrappers around
these three.

## 64. Final pipeline statement (v1, supersedes §3's baseline pipeline as the actual build target)

The final core is not:
```text
Context → giant research infrastructure → Cite
```
It is:
```text
Context
   ↓
Search
   ↓
Real source
   ↓
What can we actually read?
   ↓
Does that retained evidence answer this context?
   ↓
YES → Cite
NO  → reject
⊥   → HOLD
```
This puts every resource where hallucination actually turns from "a model's
idea" into "a citation the user received" — directly at that boundary.

## 65. Additional references (Part II)

References 10–13 (Crossref REST API, ThaiJO homepage, OpenAlex Works
Overview, ThaiJO downloads page) were already listed in §43 — not repeated
here to avoid duplicate entries. Continuing the numbering:

14. `main.hub/ROUTES.md` (Step 0 — finite-readout discipline):
    https://github.com/morrocwi/main.hub/blob/main/ROUTES.md
15. `main.hub/SURFACES.md` (Skill/MCP/CLI/API as separate surfaces, no
    always-on-service requirement):
    https://github.com/morrocwi/main.hub/blob/main/SURFACES.md
16. `main.hub/GOAL.md` (cut ceremony that doesn't change behavior):
    https://github.com/morrocwi/main.hub/blob/main/GOAL.md
17. `readout-universe` SKILL.md (evidence-tagging discipline, resolution-aware
    claims):
    https://github.com/morrocwi/readout_universe/blob/eafbf2e4ada7f6cd61a7d85b5203368af0d46a65/plugins/readout-universe/skills/readout-universe/SKILL.md
18. `skillme` SKILL.md (NOT_FOUND ≠ CANNOT_VERIFY; separate metadata
    verification from scope verification):
    https://github.com/morrocwi/skillme/blob/208e76dfae692396fbf54ae64119978020275044/plugins/skillme/skills/skillme/SKILL.md
19. `dual-lane-epistemic-harness` README (fail closed for truth, remain open
    for imagination; imagination lane vs. authorized lane):
    https://github.com/morrocwi/dual-lane-epistemic-harness/blob/1507931b96456e4dd983adfde0637b1c6bc48fca/README.md

---

# PART III — Market-Informed Repositioning (adds a competitive lens; does not weaken Parts I–II)

> **Relationship to Parts I–II:** everything below was produced by checking
> the design against the actual 2026 market (Elicit, Consensus, Scite,
> ResearchRabbit, Connected Papers) rather than assuming a gap exists. It
> does not change any invariant in §4a, and it does not replace the Cite
> Admission Gate from Part II — it **completes** one field that Part II's
> Evidence Capsule (§58) already sketched with a single example
> (`scope.relation: SUPPORTS`) but never fully enumerated, and it **adds** one
> new pre-admission check (Source Safety, §77) that Part II did not have. See
> §65a for the exact term-by-term reconciliation.
>
> **A note on the numbers below (corpus sizes, article counts):** these are
> as reported by each vendor's own marketing/help pages or by government
> portal counters (ThaiJO, TNRR) at the time of this review — they are cited
> here as *readouts of what was claimed*, not independently verified facts.
> Treat "138M+", "220M+", "317M+", "420,633", "537,492" etc. as "as reported,
> as of this check" rather than settled ground truth; this is consistent with
> this project's own stance that a citation must be checkable, not merely
> asserted (§4a invariant 7 applies to this document's own claims too).

## 65a. Terminology reconciliation — Part II ↔ Part III

| Part II (§44–65) | Part III (§66–83) | Reconciliation |
|---|---|---|
| Evidence Capsule `scope.relation: SUPPORTS` (single example, §58) | Full relation enum: `SUPPORTS` / `CHALLENGES` / `CONTEXT_ONLY` / `UNCLEAR` (§72) | Part III completes the enum Part II only sketched. `UNCLEAR` is new and routes to `HOLD`, same as any other unresolved state in §53. |
| 3-check Admission Gate: Identity, Content, Scope (§54) | 4-check gate: Identity, Content, Scope, **Source Safety** (§77) | Source Safety (retraction/correction/editorial-notice check) is an added check, run alongside or just before Scope, not a replacement for any of the original 3. |
| Evidence levels P/A/M — *how much* was read (§55) | Relation labels SUPPORTS/CHALLENGES/CONTEXT_ONLY/UNCLEAR — *what it says* (§72) | These are orthogonal dimensions of the same Evidence Capsule, not competing labels: a citation can be `P` (full-text passage) *and* `CHALLENGES` the context (a real paper, fully read, that contradicts the claim) — which must still `HOLD`, not `ADMIT`, even though evidence depth is maximal. Output should show both fields, per §71. |
| 4-core adapter router: OpenAlex, Crossref, ThaiJO OAI, PubMed+PMC (§56) | Same 4 core (open/free), plus an **optional premium tier** (Elicit, Scite, Consensus) a user may plug in if they hold an account (§73) | §56 is unchanged as the required open core; Part III only adds an optional additive tier, never a required dependency. |
| "Citation Firewall between AI and scholarship" framing (§50) | "Evidence Gateway for Thai & Global Scholarly Sources" / "Citation Firewall for AI Agents, with first-class Thai research coverage" (§70) | Same core framing, refined into an actual external-facing name/positioning after checking it against real competitors. |
| Public interface sketch: `thai_cite.find(context)`, optional `resolve`/`verify`/`health` (§45) | Reduced to exactly two calls: `find_cites(context)`, `verify_cite(context, citation)` (§70) | Confirms and narrows §45's sketch — `resolve`/`health` are operational, not part of the narrow public contract Part III recommends leading with. |
| Baseline pipeline variants (§3, §48, §52, §64) | 10-step pipeline with seed-driven network expansion (§79) | Adds two new stages — **Seed Selection** and **Network Expansion** (§76) — between discovery and fusion, sourced from ResearchRabbit/Connected Papers pattern; everything downstream (fusion → evidence → relation → safety → admission) matches §52/§64 with the Safety check inserted. |

## 66. Market reality check (2026) — the general search problem is already solved elsewhere

Re-checking against the current market changes the conclusion substantially:
**the system is still worth building, but not as "another AI paper-search
tool."** The 2026 international market has moved far past where the original
brief assumed.

As reported by each vendor: Elicit holds 138M+ papers and runs a staged
systematic-review pipeline (search → screening → extraction → synthesis) with
sentence-level citation grounding, now with both an API and MCP. Consensus
reports 220M+ papers, semantic + keyword search, full text from multiple
publishers, exact-quote grounding, and its own API/MCP. Scite reports 317M+
articles, 41M+ full-text sources, and 1.6 billion "Smart Citations" that
classify citation context as supporting/contrasting/mentioning, also with
API/MCP.

So several things this project originally planned to build **already exist in
the market**: natural-language paper search, semantic retrieval, citation
graphs, exact-quote grounding, MCP/API access, full-text retrieval, related
papers, and even whether other articles support or contradict a given paper.

### Direct comparison

| Dimension | Elicit | Consensus | Scite | ThaiCite (as originally conceived) |
|---|---|---|---|---|
| Natural-language → papers | Very strong | Very strong | Strong | Should not rebuild |
| Corpus size (as reported) | 138M+ | 220M+ | 317M+ | No corpus of our own |
| Full text | Partial / subscription-linked | Growing publisher access | Very strong, 41M+ | Limited to what upstream sources expose |
| Exact quote grounding | Yes | Yes | Yes (citation statements/full text) | Achievable, but not unique |
| Citation network | Some workflows | Citation Graph | Very strong | Can use upstream |
| Support/contrast classification | Via extraction/synthesis | Via synthesis | Core strength | Would trail Scite if built alone |
| API/MCP | Yes | Yes | Yes | Achievable, but not unique |
| PubMed-specific route | Yes | Combined corpus | Combined biomedical | Direct adapter achievable |
| ThaiJO as first-class source | No official evidence found | No official evidence found | No official evidence found | **Achievable directly** |
| TNRR as first-class source | No first-class route found | Not found | Not found | **Achievable directly** |
| TCI tier/context | Not a focus | Not a focus | Not a focus | **Achievable directly** |
| Thai↔English identity resolution | Not a core focus | Not a core focus | Not a core focus | **A genuine possible strength** |
| Fail-closed admission policy like ours | Has its own grounding | Has grounding | Has validation | **Can be made explicit and machine-enforced** |
| Local/open/local-MCP | Proprietary | Proprietary | Proprietary | **Achievable as open/local** |

The Thai-source gap has a real, substantial base behind it: ThaiJO currently
shows roughly 420,633 articles with direct OAI-PMH metadata harvesting; TNRR
(วช.) shows roughly 537,492 research outputs and 137,611 researchers, with an
API that requires authentication to obtain a token; TCI carries direct
article search and Thai-journal tier data (all figures as reported by those
portals at the time of this check, not independently re-counted here).

**This is a genuine gap.** But "the market has no Thai work" is not entirely
true — some ThaiJO output is already indexed by OpenAlex, and some Thai
journals report appearing in Semantic Scholar/SciSpace too. So the
differentiator is **not**:

> "We're the only ones who can find Thai papers."

It is:

> **"We treat the Thai research ecosystem as a first-class source and
> deliberately resolve it together with the global ecosystem."**

This is a much stronger claim.

## 67. Where the architecture still has real value — the problem was never `search`

Through this project's own lens, what survives after the market check is not
`search` — it's **authorization**.

Market tools are already good at answering:

> "What papers are relevant?"

Several are good enough to answer:

> "Which exact passage in the paper is relevant?"

But there is a narrower question none of them are built to answer as an
infrastructure contract:

> **"Does another AI agent have the right to take this source out and use it
> as a citation yet?"**

That becomes infrastructure:
```text
Any AI
   ↓
ThaiCite
   ↓
SEARCH upstream systems
   ↓
SOURCE EXISTS?
   ↓
CONTENT READ?
   ↓
SCOPE MATCH?
   ↓
ADMIT / HOLD
```
This is useful precisely because it means **not competing on corpus size**
with Elicit/Scite/Consensus. Instead, those systems can become **optional
upstream adapters** later:
```text
Open / free
  OpenAlex
  Crossref
  PubMed
  PMC
  ThaiJO
  TNRR

Optional premium
  Elicit
  Scite
  Consensus
```
ThaiCite becomes a **policy/verification gateway** sitting on top of all of
them. This is a stronger position than trying to out-search the market.

## 68. The biggest real weakness: full text

Scite has a large advantage from 44+ publisher partnerships and correspondingly
large full-text access (as reported by Scite). Consensus reports adding
full-text access from Wiley, AAAS, Taylor & Francis, Sage, ACS, APA, and
others through 2026. This project relies primarily on open infrastructure, so
it will regularly hit:
```text
paper exists ✓
abstract available ✓
full text ✗
```
Rule "no full text → no cite" gives high precision but very low recall. Rule
"abstract is enough → cite" improves recall but risks claim mismatch. This is
why the `P` / `A` / `M` split from Part II (§55) has real value — but the
**output must show which level applied, not hide it**:
```text
CITE-001   Evidence: P
CITE-002   Evidence: A
```
because `P` and `A` do not carry equal evidentiary strength.

## 69. A point previously overrated: passage verification alone

Having an exact passage **does not automatically fix claim misuse**. Example:
a paper states:

> "We found no significant improvement in critical thinking."

A model could select this *real* passage and still misinterpret it as
evidence for "AI improves critical thinking." So:
```text
passage exists  ≠  passage supports the claim
```
This is exactly why Scite invests heavily in classifying citation statements
as supporting/contrasting/mentioning, rather than just locating text. This
system should therefore use:
```text
SOURCE
   ↓
PASSAGE
   ↓
RELATION
   ├── SUPPORTS
   ├── CHALLENGES
   ├── CONTEXT_ONLY
   └── UNCLEAR
```
with `UNCLEAR → HOLD`. This matches this project's own lens closely (see
§72 for the formalized version of this relation set).

## 70. Retrieval quality is also a real weak point — do not compete on it

Elicit has reported evaluating its search/systematic-review pipeline against
gold-standard Cochrane reviews with strong recall on its own benchmark. This
project has no training corpus, no proprietary ranking, and no large-scale
researcher feedback loop. Writing a custom `our_semantic_search.py` to
compete with these systems solves the wrong problem. Instead, use:
```text
OpenAlex search
PubMed search
ThaiJO search
TNRR search
optional Elicit/Scite/Consensus
```
then perform **fusion + admission** on top, rather than building retrieval
from scratch.

## 71. Real advantages that survive the market check — only 4

1. **Thailand-first federation** — ThaiJO + TNRR + TCI treated as equal
   first-class sources alongside PubMed/OpenAlex/Crossref, not as a
   byproduct of a global index.
2. **Thai ↔ global identity resolution** — Thai/English titles, พ.ศ./ค.ศ.,
   DOI/no-DOI, ThaiJO/TNRR/OpenAlex duplicates, resolved deliberately.
3. **Explicit admission boundary** — other systems have their own internal
   grounding, but ThaiCite can make it an infrastructure contract: an AI
   agent **can never construct a citation itself** — it must go through the
   gateway.
4. **Open/local/vendor-neutral** — a small MCP + CLI, no vendor lock-in, and
   Elicit/Scite/Consensus can be plugged in later if the user has an account.

Items 1–2 are a domain advantage. Item 3 is an architectural advantage. Item 4
is a deployment advantage. Outside of these four, competitors are ahead.

## 72. Real, dangerous weaknesses (honest status)

- **Coverage** — world coverage will lag Elicit/Consensus/Scite if limited to
  OpenAlex/Crossref/PubMed.
- **Full text** — no publisher agreements, so passage verification is
  impossible for a large share of works.
- **Ranking** — finding "the best" work is much harder than confirming a
  paper is real.
- **Thai metadata quality** — older Thai records, author-name variants,
  พ.ศ./ค.ศ., or missing DOIs make dedup hard.
- **TNRR access** — the API is real but requires authentication/token, not a
  public anonymous endpoint.
- **Claim classification** — `SUPPORTS`/`CHALLENGES` still depends on an
  AI/classifier and can be wrong.
- **No benchmark yet** — there is currently no evidence this architecture
  selects citations better than asking Elicit/Consensus/Scite directly.

The last point matters most. Right now ThaiCite is honestly:
```text
Dr — plausible architecture
```
not:
```text
"a system proven to be better."
```
(Using this workspace's own honesty tiers: `Dr` = a plausible, reasoned
design, not yet a `Th_coqc`/`finite_diagnostic`-tier verified result.)

## 73. Formalized relation labels for the Evidence Capsule (completes §58)

```text
SOURCE
   ↓
PASSAGE
   ↓
RELATION
   ├── SUPPORTS
   ├── CHALLENGES
   ├── CONTEXT_ONLY
   └── UNCLEAR
```
`UNCLEAR` routes to `HOLD`, exactly like any other unresolved Admission Gate
state (§53). This formalizes the single example (`scope.relation: SUPPORTS`)
that §58's Evidence Capsule schema already carried — no schema change is
required, only enumerating the full label set the field was always meant to
hold.

## 74. Market repositioning — not "AI Research Search"

Launching this as "AI Research Search" invites direct competition with
well-funded, multi-year-ahead products. Better positioning:

> **ThaiCite — Evidence Gateway for Thai & Global Scholarly Sources**

or

> **Citation Firewall for AI Agents, with first-class Thai research
> coverage**

Public interface reduced to exactly two calls:
```text
find_cites(context)
verify_cite(context, citation)
```
Explicitly **not building**: a research workspace, PDF chat, a reference
manager, a citation-graph UI, or a report writer — the market already does
these better than this project reasonably could.

## 75. Is it worth building? — conditional answer

If the goal is:

> Build a product that competes globally with Elicit / Consensus / Scite

**not recommended** — corpus cost, full-text licensing, ranking, UX, and
evaluation cost are all very high, and competitors are years ahead.

If the goal is:

> Build infrastructure that lets an AI agent find and use **Thai + global**
> work in a way that is traceable and that the agent cannot fabricate on its
> own

**there is a clear case for building it** — especially because the existing
market ecosystem can be reused rather than rebuilt.

## 76. Patterns borrowed from the market, not reinvented

| Pattern absorbed | Source | How it's used in ThaiCite |
|---|---|---|
| Search → screen → evidence-extraction as separate pipeline stages | Elicit | A search result never becomes a citation directly. |
| Every conclusion must point back to a sentence/evidence in the source | Elicit / Consensus | Evidence Capsule stores passage/abstract + locator. |
| Full-text-first when reachable | Consensus / Scite | Priority order: passage > abstract > metadata. |
| Citation relation, not just citation count | Scite | `SUPPORTS / CHALLENGES / CONTEXT_ONLY / UNCLEAR`. |
| Check retraction/editorial notice/contrasting evidence before citing | Scite Reference Check | Pre-output Source Safety check (§77). |
| Walk outward from a seed paper via the citation network | ResearchRabbit | Citation-neighbour expansion once good seeds are found. |
| Don't rely on keyword search alone | ResearchRabbit | Keyword/semantic finds seeds, then graph expansion takes over. |
| Co-citation + bibliographic coupling to find related work | Connected Papers | Optional neighbourhood ranking. |
| Core recommendation doesn't need to be an LLM | ResearchRabbit | Deterministic/network ranking first, LLM rerank second. |
| AI is an assistant, never a source of truth | every mature system reviewed | AI may query/rank/classify but cannot manufacture a bibliographic fact. |

Elicit demonstrates a strong pattern: search, screening, extraction, and
synthesis are distinct stages, and extraction must be traceable back to a
quote/figure in the source. Consensus similarly grounds answers in a precise
passage and prefers full text when available. Scite's deeper lesson is not
"the paper is real" but **how other papers cite it** — supporting,
contrasting, or mentioning — plus a reference-check for
retraction/editorial-notice/contrasting evidence. ResearchRabbit's most
worth-absorbing idea is **not letting the LLM be the whole search engine** —
its core recommendation reportedly does not use an LLM at all, which is both
lighter and easier to audit. Connected Papers uses co-citation and
bibliographic coupling to find related work even without direct citation —
useful when terminology shifts across eras or fields.

## 77. Source Safety check (new — added to the Admission Gate)

Borrowed directly from Scite's Reference Check pattern: before a candidate
can be admitted, check:
```text
retraction?
correction?
conflicting metadata?
```
This runs alongside/just before the Scope check in §54, making the Admission
Gate effectively **4 checks**: Identity, Content, Scope, Source Safety — see
§65a for how this relates to the original 3-check gate.

## 78. Seed-driven discovery (network expansion, extends §52/§56's router)

Some contexts cannot be resolved from keywords alone. Once 2–3 genuinely
matching papers are found, let them reshape the query space instead of
generating more synonyms:
```text
Context
   ↓
keyword/semantic → Seed A, B, C
                     ↓
             citation neighbourhood
             (references, cited-by, co-citation, bibliographic coupling)
                     ↓
              better candidates
```
This is preferred over having an LLM generate an ever-growing list of
synonyms — it lets the citation network itself do the expansion.

## 79. Scope discipline: absorb the relation classification, don't rebuild Scite

There is no need to classify 1.6 billion citation statements — that is
company-scale infrastructure. Only the relationship that actually matters
needs classifying:
```text
user context
      ↕
candidate source
```
e.g.:
```yaml
relation:
  label: CHALLENGES
  evidence:
    text: "No significant improvement..."
    locator: Results
```
This is far lighter than Scite's full infrastructure and matches exactly
what this system needs.

## 80. What NOT to absorb, even where the market does it well

Explicitly out of scope even though competitors do these well: a full
research workspace, PDF chat, note-taking, a collections UI, a
systematic-review dashboard, or a visual paper map. None of these directly
improve the accuracy of `context → cite list`, and each would bloat scope.

## 81. The five primitives

After absorbing all of the above, the real technical core is only five
primitives:
```text
SEARCH
EXPAND
RESOLVE
EVIDENCE
ADMIT
```
AI operates around these primitives — it is not a primitive itself.

## 82. Final (market-informed) pipeline

```text
CONTEXT
   ↓
1. QUERY
   Thai + English + domain terms
   ↓
2. PRIMARY DISCOVERY
   OpenAlex / PubMed / ThaiJO / TNRR
   ↓
3. SEED SELECTION
   lexical + semantic
   ↓
4. NETWORK EXPANSION
   references
   cited-by
   co-citation
   bibliographic coupling
   ↓
5. CANDIDATE FUSION
   DOI / PMID / native ID
   ↓
6. CONTENT ACQUISITION
   full text > abstract > metadata
   ↓
7. EVIDENCE CAPSULE
   source + passage + locator
   ↓
8. RELATION CHECK
   SUPPORTS / CHALLENGES / CONTEXT_ONLY / UNCLEAR
   ↓
9. SOURCE SAFETY
   retraction? correction? conflicting metadata?
   ↓
10. ADMISSION GATE
   ADMIT / HOLD
   ↓
CITE LIST
```
No custom semantic search engine, citation graph, or LLM research agent needs
to be built from scratch — existing ecosystem operators do that work:
```text
OpenAlex → global discovery + citation graph
Crossref → DOI identity
PubMed   → biomedical retrieval
PMC      → biomedical full text
ThaiJO   → Thai journal primary source
TNRR     → Thai research output
```
ThaiCite does what nothing else does fully for this problem: **fusion +
Thailand resolution + admission.**

## 83. Final positioning statement

> Use the best search infrastructure that already exists.
> Connect the Thai ecosystem the global market covers unevenly.
> Use citation-network discovery instead of keywords alone.
> Require an evidence relation before a citation is allowed.
> HOLD when the evidence isn't enough, instead of letting AI fill the gap.

This project does not compete with Elicit, Consensus, Scite, or
ResearchRabbit — it takes what each has already proven works and uses it as a
mechanism, cutting everything that doesn't serve the core retained
difference. **ThaiCite should not be "an AI research app" — it should be
citation infrastructure that any AI research app can call.**

## 84. Next validation step (not more code)

To prove this is genuinely worth building, the next step is not writing more
code — it is building a **100–200-context Thai/English benchmark** and
comparing ThaiCite directly against Elicit, Consensus, Scite, and OpenAlex on
four variables: `paper existence`, `relevance`, `claim-support accuracy`,
`Thai-source recall`. That result will show whether there is a real product
gap here, or only an architecture that looks good on paper.

## 85. Additional references (Part III)

20. Elicit — Systematic Literature Reviews: https://elicit.com/solutions/systematic-review
21. Elicit — Evaluating Elicit's Systematic Literature Review Capabilities:
    https://elicit.com/blog/evaluating-elicit-slr
22. Consensus — "What's Changed in Consensus? (Summer '26)":
    https://consensus.app/home/blog/what-has-changed-in-consensus-summer-26/
23. Consensus — full-text feature page: https://consensus.app/home/features/full-text/
24. Consensus — product changelog: https://help.consensus.app/en/articles/11954907-consensus-product-changelog
25. Scite — Features: https://scite.ai/features
26. Scite — API: https://scite.ai/api
27. ThaiJO homepage (article-count reference for this section):
    https://www.tci-thaijo.org/
28. TNRR — Thai National Research Repository homepage:
    https://tnrr.nriis.go.th/
29. TNRR — API manual (PDF; authentication/token required, not public
    anonymous): https://app.nriis.go.th/cdn/tnrr/files/API_TNRR.pdf
30. TCI — Thai-Journal Citation Centre journal list: https://tci-thailand.org/journal_list
31. OpenAlex — example work already indexed from Thai-related research
    (illustrative, not a claim of ThaiJO-wide coverage):
    https://openalex.org/W7165365445
32. ResearchRabbit — "How ResearchRabbit uses AI":
    https://learn.researchrabbit.ai/en/articles/13545485-how-researchrabbit-uses-ai
33. Connected Papers — About: https://www.connectedpapers.com/about/

---

# PART IV — Readout-Gated Citation (the single innovation underneath all of Parts I–III)

> **Relationship to Parts I–III:** this part does not add another layer of
> scope — it names and formalizes the one real design innovation that all
> the previous parts were converging on: **a citation is not a paper, it is
> a paper-used-for-a-specific-claim, carrying its own proof.** It sharpens
> three things Parts II–III left slightly open: (1) the Evidence Capsule
> (§58) becomes keyed by **(source, claim)**, not by source alone; (2) the
> 2-way `ADMIT`/`HOLD` outcome (§53) becomes 3-way `ADMIT`/`REJECT`/`HOLD`;
> (3) the AI boundary (§27, §57) is split into three explicit roles (API /
> LLM / deterministic Gate) so the LLM is never the thing that grants
> admission. See §85a for the full term-by-term reconciliation. Not claimed
> as academically novel without a prior-art review — this is a **design
> synthesis extracted from this workspace's own existing systems**
> (`main.hub`, `glosa`, `skillme`, `dual-lane-epistemic-harness`,
> `readout-universe`), not an assertion of new research.

## 85a. Terminology reconciliation — Part III ↔ Part IV

| Part III (§66–85) | Part IV (§86–102) | Reconciliation |
|---|---|---|
| Evidence Capsule, one per candidate source (§58, §73) | **Cite Card**, keyed by **(source, claim)** — one card per citation *use*, not per paper (§86–87) | A single paper can produce multiple Cite Cards, one per distinct claim it's being tested against — `PMID:123456 × Claim-A` and `PMID:123456 × Claim-B` are different cards with potentially different outcomes. The Evidence Capsule fields (source_id, metadata, evidence level/text/locator) are preserved unchanged *inside* the Cite Card (§100); nothing from §58's schema is discarded. |
| Admission Gate outcome: `ADMIT` / `HOLD` (§53, §82) | 3-way outcome: `ADMIT` / `REJECT` / `HOLD` (§89) | `HOLD` keeps its Part II/III meaning exactly: *insufficient access/resolution to decide* (paywall, fetch timeout, ambiguous relation). `REJECT` is new: evidence *was* read and clearly does not support this specific claim use (e.g. `CHALLENGES` when the caller wanted `SUPPORTS`). The same source can be `REJECT` for one claim and `ADMIT` for another — this is the direct consequence of keying by (source, claim). |
| Relation labels `SUPPORTS`/`CHALLENGES`/`CONTEXT_ONLY`/`UNCLEAR` (§73) | Unchanged — now explicitly the `relation.label` field inside a Cite Card, and `UNCLEAR` routes to `HOLD`, `CHALLENGES`-when-claim-wanted-`SUPPORTS` routes to `REJECT` (§89) | No new labels; this just wires the existing enum into the sharper 3-way gate outcome. |
| AI boundary: AI can query/rank/classify, never manufacture a bibliographic fact (§27, §57) | Explicit 3-role split: **API** = evidence, **LLM** = interpretation only (context decomposition, query generation, relation labeling), **deterministic Gate** = the only thing that sets `ADMIT`/`REJECT`/`HOLD` (§98) | Sharper version of the same rule. The `decision` field in a Cite Card is *never* set by the LLM even when the LLM supplied `relation.label` — a plain rule (§98) consumes `relation.label` plus the boolean checks to compute `decision`. |
| Evidence levels P/A/M → output policy "P and A may output, M must never" (§55, §68) | Authorization policy: `P + clear relation → ADMIT (strong)`, `A + clear relation → ADMIT_WITH_LIMITS`, `M → HOLD` (§90) | Refines the binary "may output" rule into a graded one — `ADMIT_WITH_LIMITS` is new, meaning admitted but the output should visibly flag it as abstract-only evidence, matching §71's "show P vs A in output" instruction. |
| Seed-driven network expansion (§78), 4-core router (§56) | Global × Local two-track retrieval, explicit `LOCAL_EVIDENCE_NOT_FOUND ≠ NO_LOCAL_EVIDENCE_EXISTS` (§92) | Formalizes the same OpenAlex/PubMed/Crossref-vs-ThaiJO/TNRR split already implicit in §56 and §66, adding the explicit non-existence-vs-not-found distinction from `skillme`/§19/§51's `0 ≠ ⊥`. |
| Query Planner (§9) | Context Contract, frozen before search, versioned if it must change (§93) | Adds a small pre-search artifact and a lineage rule; §9's query-family output becomes one field (`intended_relation`, population, geography, etc.) generated *from* the frozen contract, not a replacement for it. |
| Honest weaknesses / no benchmark yet (§72) | Concrete negative-control checklist a working gate must pass (§94) | Turns the honest "we haven't proven this works" admission into a falsifiable test list — directly comparable to the concept-validation workflow already running (`docs/HANDOFF_2026-09-20.md`), which already covers several of these controls (fabricated DOI, timeout-vs-NOT_FOUND, semantic-trap). |
| — (not previously addressed) | Time-bound verification: `VERIFIED AT <date>`, not forever; `SUPERSEDED`/`SCRAMMED` states on re-check (§95) | New addition, not a revision of anything earlier. |

## 86. The core insight — a citation is a "Citation Use," not a Paper

Most systems implicitly model:
```text
Paper → Citation
```
This project should model:
```text
Paper
 + the Claim it is about to support
 + the Passage actually read
 + the relationship between Passage and Claim
 + how it was checked
 + when/which version it was checked at
────────────────────────────
= Citation Use
```
This is a real difference. Take one paper, `PMID:123456`, and claim A:

> "AI reduces students' time-on-task."

This might resolve as:
```text
PMID:123456 × Claim-A → SUPPORTS → ADMIT
```
The same paper against claim B:

> "AI increases critical thinking."

might resolve as:
```text
PMID:123456 × Claim-B → UNCLEAR → HOLD
```
So: **this project does not verify a "paper" — it verifies a "use of a
paper."** This is already explicit in this workspace's `glosa` methodology,
whose `citation_card.schema.json` is defined as **one card per citation USE**,
and which keeps `metadata_verified` and `claim_match_verified` as separate
fields rather than one blended "verified" flag. This should become the heart
of ThaiCite's object model.

## 87. Innovation 1 — Proof-Carrying Citation

Borrowed from `main.hub`'s own rule that an edge is never permitted just
because two things are asserted to be connected — it requires a file plus
literal evidence a checker can independently open and verify. The same
principle applied to a citation:
```yaml
cite_use_id: TCITE-U-...

source:
  kind: PMID
  id: "12345678"

claim:
  hash: sha256(...)
  text: "..."

evidence:
  fetched_from: "..."
  fetched_at: "..."
  content_hash: "..."
  locator: "Results, paragraph 3"
  passage: "..."

relation: SUPPORTS

verification:
  metadata: PASS
  claim_match: PASS

admission: ADMIT
```
A citation becomes an **object that carries its own reason for having the
right to appear in output** — not just `Smith et al. (2024)`.

## 88. Innovation 2 — Dual-Lane Citation Architecture

From `dual-lane-epistemic-harness` (already applied once in §53): imagination
is never forbidden, only authorization is restricted. Applied at the
citation-object level rather than just the state-machine level:
```text
                AI / Search
                    │
           DISCOVERY LANE
                    │
     candidate candidate candidate
                    │
                    ▼
             EVIDENCE BOUNDARY
                    │
                    ▼
          AUTHORIZATION LANE
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
      ADMIT       REJECT       HOLD
```
An LLM may propose 1,000 candidate papers — no problem. **Zero** of them are
required to leave the system if the evidence isn't there. This differs from
trying to "reduce model hallucination" — the goal is not to make the model
stop hallucinating (an unsolved, arguably unsolvable, generative-model
property); the goal is that **hallucination is never granted the privilege of
crossing the boundary.** This framing is substantially stronger than trying
to fix the model itself.

## 89. Innovation 3 — HOLD as a first-class result (now 3-way: ADMIT/REJECT/HOLD)

Most systems are forced into a binary yes/no. This project's lens preserves
`0 ≠ ⊥` (§19, §51), so ThaiCite keeps three distinct outcomes:
```text
ADMIT   — enough evidence exists to use this citation for this claim
REJECT  — the evidence actually read shows this source does not fit this claim
HOLD    — cannot yet decide, given the access/resolution actually available
```
Example — a paywalled paper:
```text
metadata ✓
abstract ✗
full text ✗
→ HOLD
```
This is neither `REJECT` (we have no evidence it's wrong — we simply
couldn't read enough) nor `ADMIT` just because the title looks like a fit.
This directly fixes a common AI failure mode: treating "I can't check" the
same as either "yes" or "no."

## 90. Innovation 4 — Evidence Resolution is graded, not binary verified/unverified

Applying `readout-universe`'s discipline (already introduced as P/A/M in
§55): the evidence level and the relation label combine into a graded
authorization policy, not a flat "verified":
```text
P — PASSAGE:  full text read, passage located
A — ABSTRACT: abstract read
M — METADATA: only know the work is real
```
```text
P + relation clear   → ADMIT (strong)
A + relation clear   → ADMIT_WITH_LIMITS
M                    → HOLD
```
So the system never says "verified" as if every method of checking carried
equal strength — `ADMIT_WITH_LIMITS` output should visibly flag that only
abstract-level evidence backed it (extends §71's "show P vs A in output").

## 91. Innovation 5 — Adversarial Search Pair (support × challenge)

`glosa`'s literature-review methodology (`search_log.schema.json`, the LRS
process) requires separating **support queries** from **challenge queries**,
and a challenge query is never allowed to be merely `NOT <support query>`.
Adapted for ThaiCite: for context

> "social media causes depression"

the system does not search only:
```text
social media depression association
```
it must also generate:
```text
SUPPORT
  social media depression risk
  social media mental health longitudinal

CHALLENGE
  social media depression null association
  social media mental health confounding
  social media depression bidirectional causality
```
so the candidate pool is never built from only one side of the framing from
the start. Several market tools surface "contrasting citations" as a
downstream feature; this project instead injects the challenge framing at
**query-generation time** — a mechanically different (and earlier) point of
intervention.

## 92. Innovation 6 — Global × Local Two-Track Retrieval

The retrieval structure best suited to this project specifically:
```text
              CONTEXT
                 │
        ┌────────┴────────┐
        ▼                 ▼
 GLOBAL TRACK         LOCAL TRACK
 OpenAlex             ThaiJO
 PubMed               TNRR
 Crossref             TDC/TCI
        │                 │
        └────────┬────────┘
                 ▼
           evidence fusion
```
not "search the world and hope Thai work happens to surface." Carrying over
`glosa`'s core semantic distinction:
```text
LOCAL_EVIDENCE_NOT_FOUND   ≠   NO_LOCAL_EVIDENCE_EXISTS
```
This lets ThaiCite not just find Thai work, but **honestly preserve the
asymmetry between global and local coverage** instead of silently treating
"we didn't find a Thai source" as "no Thai source exists."

## 93. Innovation 7 — Frozen Context Contract before retrieval

A lightweight version of PRISMA-style scope-freezing, without building a full
PRISMA workflow every time. Before searching, create a small object:
```yaml
context_contract:
  claim: "..."
  intended_relation: evidence_for
  population: "..."
  geography: Thailand
  timeframe: null
  language: [th, en]
  created_before_search: true
```
then search. This prevents an AI from quietly reshaping the question to chase
a paper it just happened to find. If the contract must change:
```text
Context Contract v1 → superseded by v2
```
with lineage kept — a lightweight anti-cherry-picking mechanism.

## 94. Innovation 8 — Fail-able Citation Gate (negative-control checklist)

`readout-universe` holds that a gate claiming to be evidence-bearing must
demonstrably pass what should pass **and reject what should be rejected** —
not just be tested on the happy path. ThaiCite's gate must pass negative
controls such as:
```text
Fake DOI                          → HOLD/REJECT
Real DOI + invented title          → REJECT
Real paper + unrelated claim        → REJECT/HOLD
Paper contradicts claim              → CHALLENGES, not SUPPORTS
API timeout                           → HOLD, not NOT_FOUND
Paywall                                → HOLD
Secondary citation only                 → HOLD
Retracted paper                          → blocked / explicit status
```
Only once these pass does it become reasonable to say the admission gate
"works." This reliability work matters more than adding ten more databases.

> **Tie-in to the running validation:** the concept-validation workflow
> already in progress (`docs/HANDOFF_2026-09-20.md`, run `wf_3b5765ac-594`)
> already exercises several of these exact controls — its
> `FABRICATED_WORK`/`SEMANTIC_TRAP` categories cover "fake DOI"/"real DOI +
> invented title," and its `ERROR_STATE` category covers "API
> timeout → HOLD, not NOT_FOUND." It does not yet cover "paper contradicts
> claim → CHALLENGES" or "retracted paper → blocked," since those depend on
> the relation-labeling (§73) and Source Safety (§77) machinery that postdate
> when the run was scaffolded — worth a follow-up validation pass once §86–99
> are actually implemented.

## 95. Innovation 9 — Verification is time-bound

`main.hub` teaches that a pin is a readout of "one point in time," and if the
target repository has since changed, the target wins over the hub's cached
pin. The same applies here:
```text
VERIFIED AT 2026-09-20
```
never
```text
VERIFIED FOREVER
```
because a paper can later be corrected, retracted, superseded, or have its
metadata corrected. A load-bearing citation can therefore be re-checked
before a high-stakes export/publish:
```text
stored verification
       ↓
is stale?
       ↓ yes
re-resolve source
       ↓
ADMIT / HOLD / SCRAMMED
```
`glosa` already carries `SCRAMMED` and `SUPERSEDED` as citation states —
reused here rather than invented fresh.

## 96. One name for all nine innovations — Readout-Gated Citation

Collectively these are not nine separate systems but one:

> **Citation Authorization Protocol**, or — preferred —
> **Readout-Gated Citation**

```text
RAW CONTEXT
     │
     ▼
CONTEXT CONTRACT
     │
     ▼
GLOBAL + LOCAL
SUPPORT + CHALLENGE
DISCOVERY
     │
     ▼
SOURCE-NATIVE RECORD
     │
     ▼
EVIDENCE CAPSULE
     │
     ├─ identity
     ├─ fetched content
     ├─ locator
     ├─ passage
     ├─ resolution
     └─ timestamp/hash
     │
     ▼
CLAIM ↔ EVIDENCE RELATION
     │
 ┌───┼────────┬─────────┐
 ▼   ▼        ▼         ▼
SUPPORT CHALLENGE CONTEXT UNCLEAR
     │
     ▼
ADMISSION GATE
 ┌───────┼────────┐
 ▼       ▼        ▼
ADMIT   REJECT   HOLD
     │
     ▼
CITATION LIST
```
What the user sees stays simple:
```text
1. Cite ...
2. Cite ...
3. Cite ...
```
but every entry carries a **proof trail** behind it.

## 97. What to actually build in v1 — 5 priority innovations only

To keep "as light as possible, addressing the most impact": build only these
five first:
1. **Citation-use object** — verify per-claim, not per-paper.
2. **Evidence Capsule / proof-carrying cite** (now embedded in the Cite Card).
3. **ADMIT–REJECT–HOLD dual-lane gate.**
4. **Support × Challenge query generation + Global × Local search.**
5. **Fail-able negative-control benchmark** (§94's checklist).

Explicitly **not yet built**: a public TCITE ID, this project's own citation
graph, a large local corpus, a vector DB, full systematic-review
infrastructure, or any UI. These five attack the actual problem most
directly:

> AI can find anything it wants.
> But before that thing becomes "a citation a human sees," it must show its
> identity, show what was actually read, show its relationship to the claim,
> and pass the gate.

This differs meaningfully from "an AI that hallucinates less" — the goal is
not to change the generative model's nature, but to **design a structure that
never requires scholarship to trust the model at any point where trust isn't
actually necessary.**

## 98. Role separation — API / LLM / deterministic Gate

The core must not be "API + LLM" alone — if the LLM is the final decision-
maker, hallucination simply moves from the search stage to the verification
stage instead of being eliminated as a privilege. Roles:
```text
API   = "what did the outside world actually give us"
LLM   = "what does what we got mean, relative to the context"
GATE  = "given what we have, are we allowed to release this as a cite yet"
```
`relation.label` may come from the LLM, but `decision` is never something the
LLM sets itself. Example deterministic rule:
```text
IF source_exists = true
   AND identifier_resolved = true
   AND content_fetched = true
   AND relation ∈ {SUPPORTS, CHALLENGES}
   AND relation_confidence >= threshold
THEN ADMIT
```
If `relation = UNCLEAR` → `HOLD`. If `source_exists = true` but the passage
contradicts the claim → `REJECT` for use as a `SUPPORT`, while the *same*
paper can simultaneously become `ADMIT` for use as a `CHALLENGES` citation —
this is exactly why Citation Use (§86) matters more than Paper Verification.

### Adapters return one common shape regardless of source

```yaml
source_record:
  source:
  native_id:
  identifiers:
  title:
  authors:
  abstract:
  fulltext_locations:
  fetched_at:
```
so the core never needs to know whether a record came from PubMed or ThaiJO.

### LLM is used in exactly 3 places

1. **Context decomposition** — raw text → claim/concepts/population/
   location/outcome.
2. **Query generation** — Thai + English, support + challenge, global +
   local.
3. **Claim ↔ evidence relation** — read a passage, classify as
   `SUPPORTS`/`CHALLENGES`/`CONTEXT_ONLY`/`UNCLEAR`.

The LLM must never: invent a DOI, invent a paper, edit fetched metadata,
declare "not found" to mean "does not exist," or set `ADMIT` itself.

## 99. The Cite Card as a traveling object

Rather than assembling a citation object only at the end, it is created at
candidate stage and accumulates fields as it moves through the pipeline:
```text
CARD CREATED
│
├── discovery
│      source_id added
│
├── resolution
│      DOI/PMID added
│
├── fetch
│      abstract/passage added
│
├── relation
│      support/challenge added
│
├── gate
│      ADMIT/HOLD/REJECT
│
└── output
       citation formatted
```
Provenance is built *alongside* the work, not reconstructed afterward. This
also means one adapter failing does not break the whole card — e.g. OpenAlex
found the work, Crossref confirmed it, but the publisher fetch timed out: the
card simply ends at
```text
HOLD
reason: CONTENT_NOT_OBTAINED
```
rather than disappearing from the system — matching `0 ≠ ⊥` exactly (§19,
§51, §89).

## 100. Cite Card schema (full)

```yaml
cite_use:
  context:
    claim: "AI improves students' critical thinking"

  source:
    doi: "10.xxxx/abc"
    pmid: null

  metadata:
    title: "..."
    authors: [...]
    year: 2025

  evidence:
    level: passage
    text: "..."
    locator: "Results, paragraph 3"

  relation:
    label: SUPPORTS
    assessed_by: llm

  checks:
    source_exists: true
    identifier_resolved: true
    content_fetched: true
    metadata_consistent: true

  decision: ADMIT
```
This unifies §87's `cite_use_id`-based object and §90's evidence-level model
into one schema: `checks.*` are booleans the deterministic Gate consumes
directly (§98); `relation.label`/`relation.assessed_by` records what the LLM
contributed without letting it touch `decision`.

## 101. System equation

One request, end to end, stays light — no large research database required:
```text
Context
  ↓
LLM decomposes into queries
  ↓
API searches 30–100 candidates
  ↓
dedup
  ↓
select top 10–20
  ↓
API/fetch opens abstract/full text
  ↓
LLM reads evidence, labels relation
  ↓
deterministic Gate
  ↓
ADMIT 6 / HOLD 3 / REJECT 11 (illustrative split)
  ↓
user sees only ADMIT
```
The rest can be discarded/cached rather than surfaced. In one line:
```text
ThaiCite = APIs (evidence) + LLM (interpretation) + deterministic Gate (authorization)
```
and the **Cite Card is the object that travels through all three layers**
until it lands on `ADMIT`, `REJECT`, or `HOLD`.

## 102. Additional references (Part IV)

`main.hub/ROUTES.md`, `skillme` SKILL.md, and `dual-lane-epistemic-harness`
README were already listed in §65 (items 14, 18, 19) — not repeated here.

34. `main.hub/AGENTS.md` (hub-as-pointer, target-repo-wins, pin-is-a-readout-
    of-one-point-in-time):
    https://github.com/morrocwi/main.hub/blob/main/AGENTS.md
35. `glosa` citation card schema (one card per citation USE;
    `metadata_verified` vs `claim_match_verified` as separate fields):
    https://github.com/morrocwi/glosa/blob/main/schema/citation_card.schema.json
36. `glosa` search log schema (support/challenge query separation):
    https://github.com/morrocwi/glosa/blob/main/schema/search_log.schema.json
37. `glosa` Literature Review System methodology, P13 (freeze scope before
    seeing results; global + local tracks):
    https://github.com/morrocwi/glosa/blob/main/methodology/P13_literature_review.md

---

# PART V — Honest Novelty Check (downgrades the innovation claim by one level; keeps the kernel)

> **Relationship to Part IV:** this part is a deliberate self-correction, not
> a retraction. After checking Part IV's nine "innovations" against the
> actual 2026 prior-art landscape, most of them turn out to already exist as
> shipped features elsewhere. What survives is narrower and more precise than
> Part IV claimed — but it is real. See §102a for exactly what downgrades and
> what survives untouched. This is the same epistemic discipline this whole
> document has tried to apply to citations (§4a, invariant 7 — "every claim
> must be traceable") applied reflexively to the architecture document
> itself: a design claim that hasn't been checked against prior art is not
> yet entitled to call itself an innovation.

## 102a. Terminology/claim reconciliation — Part IV ↔ Part V

| Part IV claim (§86–102) | Part V correction | What survives |
|---|---|---|
| "Proof-Carrying Citation" (§87) as an innovation | **Not novel at component level** — RefLens (AAAI 2026) already does evidence-grounded citation verification with verbatim-span extraction shown as citation-level cards (§103, §104) | The *mechanism* (fetched passage + locator + hash) stays in the design (§100's Cite Card schema is unchanged) — just not claimed as new. |
| "Dual-Lane Citation Architecture" / abstention (§88) as an innovation | **Not novel** — CiteGuard-RAG (2026) already validates between retrieval and output and chooses accept/refuse/regenerate (§103, §104) | The dual-lane *framing* (discovery lane vs. authorization lane) stays as useful internal vocabulary; "we invented abstention" is dropped. |
| "HOLD as first-class result" (§89) as an innovation | **Partially not novel** — abstention/refusal already exists in CiteGuard-RAG — but **defining `HOLD` as a citation-USE epistemic state carrying explicit provenance of what's missing**, as part of a scholarly protocol, may still be a real (unproven) contribution (§105, §107) | `HOLD` keeps its 3-way meaning from §89; the claim narrows from "we invented refusal" to "we operationalize `0 ≠ ⊥` at the citation-use level with explicit missing-evidence provenance." |
| "Support/Contradict" relation labels (§73, §91) as an innovation | **Not novel** — Scite has done supporting/contrasting/mentioning classification for years (§104) | Labels stay in the schema (§100); not claimed as new. |
| "API + LLM + Gate role separation" (§98) as an innovation | **The role separation stays novel-adjacent** — not because "LLM verifies citations" is new (it isn't — ALCE 2023, RefLens, CiteGuard-RAG all do LLM-assisted verification) but because **explicitly forbidding the LLM from ever setting `decision`, as a protocol-level privilege boundary**, is a sharper claim than most systems make explicit (§105, §107) | §98's rule (`relation.label` from LLM, `decision` never from LLM) is kept, reframed as "epistemic privilege separation," not as "using an LLM to check citations." |
| "Citation Use = Claim × Source × Evidence × Context" (§86) as the core insight | **This is the part most likely to be genuinely under-explored** — most market tools still verify at the paper or manuscript-citation level, not the claim-source pairing level; a recent legal-citation study found models are good at "wrong case cited" but weak at "does this page support the proposition," which is exactly the paper-vs-claim-support distinction this project's model targets (§105, §110) | Kept as the central hypothesis, restated formally in §110. |
| Thai-first federation + claim-level gate (Part III §67–71, Part IV throughout) | **Still likely a real product gap in Thailand specifically** — this search did not find a Thai system doing claim-level citation authorization end-to-end; stated honestly as "not found in this search," not "does not exist" (§108) | Unchanged — this remains the strongest, least contested differentiator. |
| Overall framing: "Part IV's 9 innovations" | Downgraded to: **component-level technology is not new; architecture/protocol-level composition is unproven but plausible; Thai product-market fit is comparatively strong; scientific/patent novelty is undetermined without further work** (§109) | The document keeps all of Part IV's mechanisms (nothing is deleted from §86–102) — only the *claim of originality* attached to them changes. |

## 103. Prior art landscape (2026) — most components already exist

Re-checking against the current market and literature, most of Part IV's
individual mechanisms have fairly direct prior art. As reported by each
source:

- **RefLens (AAAI 2026)** already does evidence-grounded citation
  verification, extracting verbatim spans from the source and presenting them
  as citation-level cards.
- **CiteWell** already checks both reference existence/metadata *and*
  whether manuscript text is actually supported by the cited source.
- **CiteGuard-RAG (2026)** already validates between retrieval and output,
  choosing accept/refuse/regenerate.
- **Scite** already classifies citation context as
  supporting/contrasting/mentioning with citation statements, plus a
  reference-check for retractions/editorial notices.
- **Elicit** already ties source extraction to a quote/figure with
  sentence-level citations; **Consensus** already runs a multi-step agent
  with DOI lookup, citation traversal, full-text search, and API/MCP.
- On the research side, **ALCE** (2023) framed citation
  correctness/support as a problem from early on, and newer work such as
  **GhostCite/CiteVerifier** already systematically checks fabricated
  citations.

So a paper or pitch claiming "we built a system that uses API+LLM to verify
citations before output" is **not novel enough** on its own.

## 104. What NOT to claim as innovation (explicit cut list)

Drop these claims entirely — each has direct, shipped prior art:
```text
"Citation Card"        — RefLens already has citation-level cards
"Support / Contradict" — Scite has done this for years
"API / MCP"             — Consensus, Scite, Elicit all have this
"LLM verifies citations" — a large existing body of work already does this
"Abstention"              — CiteGuard-RAG and other evidence-grounded systems already do this
```
None of these should appear in a novelty claim going forward. They remain
part of the design (unchanged in §86–102) — they are simply not the
project's contribution.

## 105. What is starting to look genuinely novel — the unit being verified

Most of the market still models:
```text
paper
  or
citation in manuscript
→ verify
```
This project's `glosa`-derived model is different:
```text
Citation Use = Claim × Source × Evidence Passage × Context
```
meaning a paper is never `VERIFIED` in isolation. The same paper:
```text
Source X × Claim A → ADMIT
Source X × Claim B → REJECT
Source X × Claim C → HOLD
```
This matters because citation *validity* (does the source exist, is it
correctly identified) and citation *support* (does the cited page actually
back this specific proposition) are genuinely different problems — recent
legal-citation research found models are considerably better at catching
"wrong case cited" than at judging whether the cited page actually supports
the proposition being made. This gives the Citation-Use unit an actual
grounded rationale, not just a schema design choice.

## 106. The Readout-Gated Citation Protocol (consolidated diagram)

This refines §96's diagram into its clearest form — same protocol, tighter
presentation:
```text
                    GENERATIVE / DISCOVERY
Context ──→ Search/APIs/LLM ──→ Candidate Cite Uses
                                   │
                         ──────────┼──────────
                            authorization boundary
                                   │
                                   ▼
                         SOURCE-NATIVE RECORD
                                   │
                              content read
                                   │
                         claim ↔ evidence relation
                                   │
                       ┌───────────┼───────────┐
                       ▼           ▼           ▼
                     ADMIT       REJECT       HOLD
```
The novelty question, if there is one, sits in **composing these three things
together**, not in any one of them alone:

**1. Citation-use authorization, not paper verification.** The system never
asks "is this paper good?" — it asks "given the evidence we can currently
access, does this source have the right to be used for this specific claim,
right now?" This is the sharpest distinction available.

**2. `HOLD` as a real epistemic state, not an error.**
```text
ADMIT  = the evidence read supports this use
REJECT = the evidence read contradicts/does not support this use
HOLD   = the resolution we have cannot yet decide
```
Operationalizes `0 ≠ ⊥` and the dual-lane framing already present in this
workspace's own ecosystem (§53, §88). CiteGuard-RAG already has
abstention/refusal, so "a system that can refuse" is not new — but defining
`HOLD` as a citation-use state carrying explicit provenance of *what is
missing*, as part of a scholarly protocol, may be a distinct contribution if
it actually proves out.

**3. Authority transition kept separate from the LLM.** More significant than
"we use an LLM to check citations":
```text
API  → what exists
LLM  → interpret relation
Gate → authorize
```
The LLM has no method like `set_status("ADMIT")` — it may only propose
`relation = SUPPORTS` plus supporting evidence; the state machine alone
decides. This is **epistemic privilege separation**, and is a sharper,
protocol-level claim than "an LLM checks the citation."

## 107. The Thai gap remains the strongest, least contested differentiator

Thailand's infrastructure is real but serves different functions:
ThaiJO exposes OAI-PMH for systematic article-metadata harvesting; TNRR/NRIIS
is a repository of projects, research outputs, researchers, and theses with
an API reachable through a service-request process; TCI is citation-index /
journal-quality infrastructure with article/journal search; TDC aggregates
theses and research reports from many Thai institutions.

This search did not find a Thai system doing claim-level citation
authorization shaped like:
```text
Thai context
→ Thai + global sources
→ exact evidence
→ claim-source relation
→ ADMIT / REJECT / HOLD
→ callable by AI through API/MCP
```
The honest framing is **"not found in this search"**, not "does not exist in
Thailand" — consistent with this project's own `NOT_FOUND ≠ DOES_NOT_EXIST`
rule (§4a invariant 6) applied to its own competitive research. This makes
the Thai product-market case noticeably clearer than the global
scientific-novelty case.

## 108. Four-level novelty assessment (honest)

| Dimension | Current assessment |
|---|---|
| Basic underlying technology | **Low** — API/RAG/LLM/citation-verification components already exist and ship in multiple products. |
| Architecture novelty | **Medium–high**, *conditionally* — only if Citation-Use authorization, the explicit authorization boundary, and tri-state (`ADMIT`/`REJECT`/`HOLD`) semantics are locked down and actually demonstrated to produce different results than existing tools. |
| Product innovation in Thailand | **High** — from Thai-first federation combined with a claim-level gate; no equivalent found in this search. |
| Scientific novelty (publication-grade) | **Unproven** — requires benchmarking against prior art, not just architecture description. |
| Patent novelty | **Undetermined** — requires a separate, dedicated patent/prior-art search; not assessed here. |

## 109. The actual kernel hypothesis (what's worth defending)

State the claim precisely, not as a feature list:

> **A citation should not be authorized as a property of a paper.
> Authorization should be a time-bound property of a specific
> source–claim–evidence relation, under an explicit access resolution.**

In architecture language:
```text
Authorization =
f(
  claim,
  source_identity,
  evidence_passage,
  access_resolution,
  relation,
  verification_state,
  time
)
```
not:
```text
verified_source = true
```
This is the kernel worth holding onto — it follows directly from `main.hub`,
`glosa`, `readout-universe`, `skillme`, and `dual-lane-epistemic-harness`
(all already cited in §65, §85, §102), but it is not simply copying a market
feature. This function signature is the formal restatement of §86's Citation
Use definition and §98's role separation, now expressed as one hypothesis
rather than a set of independent "innovations."

## 110. Making it provable — build the benchmark before writing the paper

Do not write an architecture paper first. Build a benchmark designed to make
existing prior art fail, or at least reveal failure modes this project's gate
closes:

### 500 Citation-Use test cases across 10 categories

```text
A. fake source
B. real source / wrong claim
C. real source / claim too broad
D. contradicting source
E. abstract supports but full text qualifies
F. paywall / insufficient access
G. API failure
H. Thai-only source
I. Thai-English title mismatch
J. same paper / different claims
```

### Compared against 6 systems/baselines

```text
LLM alone
RAG
Scite-assisted
Consensus-assisted
RefLens-like verifier
ThaiCite gate
```

### Measured on at least these 7 metrics

```text
False ADMIT rate            ← most important
False REJECT rate
Correct HOLD rate
Claim-support accuracy
Citation existence accuracy
Thai-source recall
Calibration under missing evidence
```

## 111. Success criterion

If ThaiCite significantly reduces **False ADMIT** — especially in the
insufficient-evidence and Thai-work cases — without destroying recall, the
stronger claim becomes defensible:

> The innovation is not citation search or LLM-based verification. It is a
> **citation authorization architecture.**

This is the most credible direction for a genuine contribution given
everything already available in this workspace and in the market.

## 112. References (Part V)

Scite API (§43/§85 item 26), Elicit systematic review page (§85 item 20),
ThaiJO OAI Service (§43 item 5), TNRR homepage (§85 item 28), and TCI journal
list (§85 item 30) are re-cited above but already listed — not repeated here.
Continuing the numbering:

38. RefLens: End-to-End Evidence-Grounded Citation Verification with LLM
    Agents (AAAI 2026): https://ojs.aaai.org/index.php/AAAI/article/view/42361
39. CiteWell (reference existence/metadata + manuscript-support checking):
    https://citewell.org/
40. CiteGuard-RAG: A Validation-Centered AI System for Evidence-Grounded
    Question Answering (2026, accept/refuse/regenerate):
    https://arxiv.org/abs/2609.15830
41. ALCE — "Enabling Large Language Models to Generate Text with Citations"
    (EMNLP 2023, ACL Anthology): https://aclanthology.org/2023.emnlp-main.398/
42. "Is this Citation on Point?" (legal-citation support-accuracy study):
    https://arxiv.org/abs/2608.12571
