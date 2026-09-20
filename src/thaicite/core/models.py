"""Core data model for ThaiCite v0.1.

Central invariant: AI NEVER BECOMES THE SOURCE. A `Candidate` cannot exist
without a `source_adapter` and a `source_record_id` pointing at a real
external record — this is enforced at construction time, not by convention.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


class VerificationState:
    """Enum-like set of verification states a work/citation can be in.

    Kept as plain string constants (not `enum.Enum`) so states can be
    compared and serialized trivially in fixtures/JSON without extra
    boilerplate. `ALL` is the closed set used for validation.
    """

    DISCOVERED = "DISCOVERED"
    IDENTIFIED = "IDENTIFIED"
    METADATA_VERIFIED = "METADATA_VERIFIED"
    CONTENT_FETCHED = "CONTENT_FETCHED"
    CONTEXT_MATCHED = "CONTEXT_MATCHED"
    VERIFIED = "VERIFIED"
    CONFLICT = "CONFLICT"
    NOT_FOUND = "NOT_FOUND"
    FETCH_ERROR = "FETCH_ERROR"
    REJECTED = "REJECTED"

    # Distinct error sub-states. FETCH_ERROR is the umbrella category; these
    # are the specific reasons, and they must never collapse into each other
    # or into NOT_FOUND. NOT_FOUND means "this adapter's search returned
    # zero records", not "this work does not exist" and not "we could not
    # reach the adapter to find out".
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    ACCESS_DENIED = "ACCESS_DENIED"
    PARSER_ERROR = "PARSER_ERROR"

    ERROR_STATES = frozenset({RATE_LIMITED, TIMEOUT, ACCESS_DENIED, PARSER_ERROR})

    ALL = frozenset(
        {
            DISCOVERED,
            IDENTIFIED,
            METADATA_VERIFIED,
            CONTENT_FETCHED,
            CONTEXT_MATCHED,
            VERIFIED,
            CONFLICT,
            NOT_FOUND,
            FETCH_ERROR,
            REJECTED,
        }
        | ERROR_STATES
    )


@dataclass
class Candidate:
    """One raw hit from one adapter for one query.

    A Candidate MUST carry proof of where it came from. Missing
    `source_adapter` or `source_record_id` raises immediately — there is no
    code path that lets a Candidate exist without a traceable external
    record behind it.
    """

    source_adapter: str
    source_record_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    arxiv_id: str | None = None
    issn: str | None = None
    url: str | None = None
    abstract: str | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    retrieved_at: float = field(default_factory=time.time)
    # Populated by resolve/identity.py for RANKING ONLY — see can_merge().
    similarity_hint: float | None = None
    # Populated by normalize/thai_relevance.py. Classification/tagging only
    # — non-exclusive (0+ tags), and this field must NEVER be used to admit
    # or reject a citation; see evidence/verifier.py for the actual gates.
    thai_relevance: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.source_adapter or not str(self.source_adapter).strip():
            raise ValueError(
                "Candidate.source_adapter is required — a candidate with no "
                "adapter name has no traceable source and must not be "
                "constructed (AI NEVER BECOMES THE SOURCE)."
            )
        if not self.source_record_id or not str(self.source_record_id).strip():
            raise ValueError(
                "Candidate.source_record_id is required — a candidate with "
                "no record id has no traceable source and must not be "
                "constructed (AI NEVER BECOMES THE SOURCE)."
            )


@dataclass
class CanonicalWork:
    """A work identified by merging one or more Candidates.

    `candidates` must be non-empty and every member must be a real
    Candidate (already validated by its own __post_init__), so a
    CanonicalWork can never exist without at least one real source record
    behind it.
    """

    candidates: list[Candidate]
    state: str = VerificationState.DISCOVERED
    merged_identifiers: dict[str, str] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    gate_results: dict[str, bool] = field(default_factory=dict)
    # Debug evidence for G6 (candidate-vs-query identity check): shared
    # title tokens, similarity ratios, author-surname-match flag. Populated
    # by evidence/verifier.py::verify(); empty until verify() has run.
    g6_identity_debug: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError(
                "CanonicalWork.candidates must be non-empty — a canonical "
                "work with no backing candidate has no source record."
            )
        if self.state not in VerificationState.ALL:
            raise ValueError(f"Unknown verification state: {self.state!r}")

    @property
    def primary(self) -> Candidate:
        """First candidate, used for display fields (title/authors/year)."""
        return self.candidates[0]


@dataclass
class Citation:
    """A CanonicalWork that reached VERIFIED and is fit to appear in output."""

    work: CanonicalWork
    context: str
    matched_keywords: list[str] = field(default_factory=list)
    # Mirrors work.primary.thai_relevance at construction time -- metadata
    # for the caller only, never a gate (see Candidate.thai_relevance).
    thai_relevance: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.work.state != VerificationState.VERIFIED:
            raise ValueError(
                "Citation can only be constructed from a CanonicalWork in "
                f"VERIFIED state, got {self.work.state!r}."
            )


class EvidenceLevel:
    """P/A/M-style graded evidence strength (ARCHITECTURE.md SS90/SS100).

    A simple string-constant field mirroring `VerificationState`'s style --
    not a claim that full-text passage fetching is implemented yet (it is
    not, in this prototype's adapters), only that a `CiteUse` must always
    say honestly which level of evidence backed it instead of a flat
    "verified".
    """

    PASSAGE = "PASSAGE"    # P -- full text read, passage located
    ABSTRACT = "ABSTRACT"  # A -- abstract read
    METADATA = "METADATA"  # M -- only know the work is real

    ALL = frozenset({PASSAGE, ABSTRACT, METADATA})


class RelationLabel:
    """Claim <-> evidence relation labels (ARCHITECTURE.md SS73/SS100).

    QUALIFIES (added 2026-09-20, round 4 relation-classification restructure
    -- see `evidence/relation.py` module docstring): the evidence affirms
    the claim's own directional relation, but only under a narrower scope,
    condition, or population than the claim itself states (e.g. claim "X
    improves Y" vs. evidence "X improves Y only among adults under 30").
    Maps to `Decision.HOLD` in `evidence.verifier.gate_admission_decision()`
    by default -- the claim as stated is broader than what this evidence
    actually shows, which needs a human/caller decision, not an automatic
    ADMIT or REJECT.
    """

    SUPPORTS = "SUPPORTS"
    CHALLENGES = "CHALLENGES"
    CONTEXT_ONLY = "CONTEXT_ONLY"
    UNCLEAR = "UNCLEAR"
    QUALIFIES = "QUALIFIES"

    ALL = frozenset({SUPPORTS, CHALLENGES, CONTEXT_ONLY, UNCLEAR, QUALIFIES})


class Decision:
    """The 3-way Admission Gate outcome (ARCHITECTURE.md SS89/SS98).

    ADMIT  -- enough evidence exists to use this citation for this claim.
    REJECT -- the evidence actually read/available shows this source does
              NOT fit this use (a real directional mismatch, or a genuine
              G6 identity failure).
    HOLD   -- cannot yet decide, given the access/resolution actually
              available (paywall-like/transport error, unresolved
              conflict, ambiguous/UNCLEAR relation, or insufficient
              pipeline state) -- never collapsed into REJECT or ADMIT.
    """

    ADMIT = "ADMIT"
    REJECT = "REJECT"
    HOLD = "HOLD"

    ALL = frozenset({ADMIT, REJECT, HOLD})


class ContractMode:
    """DISCOVER vs VERIFY -- the two questions a `ContextContract` can be
    scoped for (2026-09-20, structural discovery/identity separation).

    A topic has no support/challenge direction; only a claim does. These
    are literally different truth-conditions, so the mode is carried as an
    explicit field rather than inferred from whether `claim` happens to be
    set:

      DISCOVER -- "what candidate knowledge is reachable for this topic?"
                  No `claim`/`intended_relation` requirement -- a topic is
                  not a proposition and has nothing to be directional
                  about. Used only by `core.engine.discover_citations()`.
      VERIFY   -- "is source X admissible evidence for claim Y?" REQUIRES
                  a real, non-empty `claim` -- see `ContextContract
                  .__post_init__`, which raises a clear error if one is
                  missing. Used only by `core.engine.resolve_citations()`
                  (`verify_cite()`'s path).
    """

    DISCOVER = "DISCOVER"
    VERIFY = "VERIFY"

    ALL = frozenset({DISCOVER, VERIFY})


@dataclass(frozen=True)
class ContextContract:
    """Frozen scope, set BEFORE search (ARCHITECTURE.md SS93).

    A lightweight anti-cherry-picking mechanism: once created, a caller
    cannot quietly reshape `claim`/`intended_relation`/scope to chase a
    paper it just happened to find -- construct a new `ContextContract`
    (a "v2") instead and keep both, rather than mutating this one (it is
    frozen for exactly that reason).

    `mode` (`ContractMode.DISCOVER` / `ContractMode.VERIFY`, 2026-09-20)
    states which of the two structurally different questions this contract
    scopes. VERIFY (the default, matching this field's pre-existing
    behavior byte-for-byte) requires `claim`; DISCOVER does not -- see
    `ContractMode`'s own docstring and `__post_init__` below.
    """

    mode: str = ContractMode.VERIFY
    claim: str | None = None
    intended_relation: str = RelationLabel.SUPPORTS
    population: str | None = None
    geography: str | None = None
    timeframe: str | None = None
    language: tuple[str, ...] = ()
    created_before_search: bool = True

    def __post_init__(self) -> None:
        if self.mode not in ContractMode.ALL:
            raise ValueError(
                f"Unknown ContextContract mode: {self.mode!r}, expected "
                f"one of {sorted(ContractMode.ALL)}"
            )
        if self.mode == ContractMode.VERIFY:
            # VERIFY answers "is source X admissible evidence for claim Y?"
            # -- that question has nothing to evaluate without a real claim,
            # so a contract with no claim must fail loudly here rather than
            # silently proceeding with an empty one (fail-closed).
            if not self.claim or not str(self.claim).strip():
                raise ValueError(
                    "ContextContract.claim is required in VERIFY mode -- a "
                    "VERIFY contract checks evidence against a proposition, "
                    "which must be stated. If you meant to ask 'what "
                    "candidate knowledge exists for this topic' instead, "
                    "construct the contract with mode=ContractMode.DISCOVER "
                    "(no claim required -- a topic has no support/challenge "
                    "direction)."
                )
            if self.intended_relation not in RelationLabel.ALL:
                raise ValueError(
                    f"Unknown intended_relation: {self.intended_relation!r}"
                )
        # DISCOVER mode: `claim` is optional and `intended_relation` is not
        # validated/required -- discovery never computes a directional
        # admission decision that would consume either (see
        # `core.engine.discover_citations()` and `DiscoveredCandidate`,
        # which has no `decision` field for one to reach in the first
        # place).


def freeze_context(
    claim: str | None = None,
    *,
    mode: str = ContractMode.VERIFY,
    intended_relation: str = RelationLabel.SUPPORTS,
    population: str | None = None,
    geography: str | None = None,
    timeframe: str | None = None,
    language: list[str] | tuple[str, ...] | None = None,
    created_before_search: bool = True,
) -> ContextContract:
    """Construct a `ContextContract`, freezing scope before retrieval.

    `mode` defaults to `ContractMode.VERIFY` (byte-identical to this
    function's pre-existing behavior for every caller that only ever
    passed `claim`) -- pass `mode=ContractMode.DISCOVER` explicitly (and
    `claim=None`, the default) to freeze a topic-only discovery contract
    instead. `language` accepts a list for caller convenience and is
    stored as a tuple so the resulting contract stays hashable/frozen
    throughout.
    """
    return ContextContract(
        mode=mode,
        claim=claim,
        intended_relation=intended_relation,
        population=population,
        geography=geography,
        timeframe=timeframe,
        language=tuple(language) if language else (),
        created_before_search=created_before_search,
    )


@dataclass
class CiteUse:
    """A Citation-Use / Cite Card: verification keyed by (work, claim), not
    by work alone (ARCHITECTURE.md SS86/SS99/SS100).

    The same `CanonicalWork` can be evaluated against multiple distinct
    claims/queries and produce a different `CiteUse` -- and potentially a
    different `decision` -- for each; nothing here mutates `work` itself
    (see `evidence/verifier.py::gate_admission_decision`, which reads
    `work.state`/`work.gate_results` but sets nothing on `work`).

    This does not replace `Citation` -- `Citation` (VERIFIED-only, one per
    work) keeps working exactly as before via `resolve_citations()`;
    `CiteUse` is an additive, richer record of one specific use of one
    specific work against one specific claim, carrying the evidence level,
    the relation label, and the final ADMIT/REJECT/HOLD decision alongside
    the underlying deterministic gate checks.
    """

    claim: str
    work: CanonicalWork
    evidence_level: str
    relation: str
    decision: str
    gate_results: dict[str, bool] = field(default_factory=dict)
    decision_debug: dict[str, Any] = field(default_factory=dict)
    evidence_locator: str | None = None
    relation_assessed_by: str = "heuristic_v1"
    query: str = ""
    context: str = ""
    matched_keywords: list[str] = field(default_factory=list)
    # Mirrors work.primary.thai_relevance at construction time -- metadata
    # for the caller only, never a gate (see Candidate.thai_relevance).
    thai_relevance: list[str] = field(default_factory=list)
    # AI-Reader-vs-deterministic-Checker transparency record ("Cite Card",
    # 2026-09-20 role-change fix -- see `evidence/verifier.py
    # ::check_claim_evidence()`, this record's source). `None` for a
    # `CiteUse` built via the plain deterministic-only path (e.g.
    # `resolve_citations()`'s current wiring, which does not yet call
    # `check_claim_evidence()` -- see the sibling MCP-contract wiring phase)
    # -- these fields are additive and do not change any existing
    # `CiteUse` construction call site's required arguments.
    ai_proposed: dict[str, Any] | None = None
    deterministic_checked: dict[str, Any] | None = None
    agreement: bool | None = None

    def __post_init__(self) -> None:
        if not self.claim or not str(self.claim).strip():
            raise ValueError(
                "CiteUse.claim is required -- a CiteUse verifies a "
                "(work, claim) pair, not a work alone."
            )
        if self.evidence_level not in EvidenceLevel.ALL:
            raise ValueError(f"Unknown evidence_level: {self.evidence_level!r}")
        if self.relation not in RelationLabel.ALL:
            raise ValueError(f"Unknown relation: {self.relation!r}")
        if self.decision not in Decision.ALL:
            raise ValueError(f"Unknown decision: {self.decision!r}")


@dataclass
class DiscoveredCandidate:
    """One real, topically-relevant candidate found under DISCOVER mode
    (`core.engine.discover_citations()`, 2026-09-20 structural fix).

    A discovery call answers "what candidate knowledge is reachable for
    this topic," never "is source X admissible evidence for claim Y" --
    those are different questions with different truth-conditions (a topic
    has no support/challenge direction; only a claim does). This type is
    STRUCTURALLY distinct from `CiteUse`, not `CiteUse` reused with
    `decision` defaulted to `None` by convention: **it has no `decision`
    field at all.** There is no `claim`/`relation`/`decision_debug` field
    either, for the same reason -- none of those concepts apply to a topic.

    `discover_citations()` never calls `evidence.verifier
    .gate_admission_decision()` or constructs a `CiteUse` anywhere on the
    path that produces this type, so it is not merely unlikely but
    IMPOSSIBLE for a `DiscoveredCandidate` to carry an ADMIT/REJECT/HOLD
    outcome -- attempting `DiscoveredCandidate(..., decision="ADMIT")`
    raises `TypeError: unexpected keyword argument 'decision'` at
    construction time, and there is no attribute to read one back from
    even by accident.

    Only carries: the identity-confirmed `work` (G1-G7 + the discovery-mode
    G6 relevance gate already passed -- see `evidence/verifier.py
    ::gate_g6_discovery_relevance`), which query-family member and context
    it was found under, the topical relevance-signal keywords, an
    `evidence_level` when a passage/abstract was actually fetched (`None`
    otherwise -- never invented), and the non-gating Thai-relevance tags.
    """

    work: CanonicalWork
    context: str
    query: str
    evidence_level: str | None = None
    matched_keywords: list[str] = field(default_factory=list)
    thai_relevance: list[str] = field(default_factory=list)
    relevance_debug: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.work.state != VerificationState.VERIFIED:
            raise ValueError(
                "DiscoveredCandidate can only be constructed from a "
                f"CanonicalWork in VERIFIED state, got {self.work.state!r}."
            )
        if self.evidence_level is not None and self.evidence_level not in EvidenceLevel.ALL:
            raise ValueError(f"Unknown evidence_level: {self.evidence_level!r}")
