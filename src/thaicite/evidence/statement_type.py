"""Statement-Type classification -- fixes the ABOUT(claim) vs.
EVIDENCE_FOR(claim) category error (P0 bug, confirmed 2026-09-20).

`evidence/relation.py::classify_relation()` only asks "does this passage
share topic terms with the claim, with no reversal signal nearby?" -- that
question conflates a passage that merely DISCUSSES/EXAMINES a claim (an
objective-statement, a hypothesis, background scene-setting, a method
description, a prior-work citation, a stated limitation) with a passage
that actually REPORTS A FINDING about the claim (a result or a
conclusion). A passage of the first kind can legitimately share every
topic term the claim uses, with no negation/reversal marker anywhere near
them, and `classify_relation()` will call that SUPPORTS -- even though no
finding was ever reported to support anything. Confirmed live:

    claim:    "social media causes depression among adolescents"
    evidence: "This study examined whether social media causes depression
              among adolescents." (an OBJECTIVE statement, no finding)
    -> classify_relation() wrongly returns SUPPORTS

    claim:    "AI tutoring improves critical thinking in students"
    evidence: "We discuss the hypothesis that AI tutoring improves
              critical thinking in students." (a HYPOTHESIS, not a finding)
    -> classify_relation() wrongly returns SUPPORTS

This module does not change `classify_relation()` at all (ABOUT-ness is a
real, separate signal it still legitimately computes). Instead it adds an
INDEPENDENT layer that answers a different question -- "what KIND of
sentence is this, rhetorically, in academic writing: does it report a
finding at all, or is it background/objective/hypothesis/method/
limitation/prior-work framing?" -- using recognizable academic-writing
lexical/phrasal cues (English and Thai), a well-known and fairly reliable
surface signal in scholarly text even without full NLU.

`evidence/verifier.py::gate_admission_decision()` combines this with
`classify_relation()`'s output: only a RESULT or CONCLUSION statement may
ever lead to ADMIT, regardless of what the relation label says. This layer
only ever NARROWS what can ADMIT -- it never widens it, and it never
itself produces REJECT (a statement that merely hasn't reported a finding
yet is a resolution/access limitation -- HOLD -- not evidence of
contradiction).
"""

from __future__ import annotations

import re
from typing import Literal

from thaicite.normalize.tokenize import tokenize as _shared_tokenize

StatementType = Literal[
    "BACKGROUND",
    "OBJECTIVE",
    "HYPOTHESIS",
    "METHOD",
    "RESULT",
    "CONCLUSION",
    "LIMITATION",
    "PRIOR_WORK",
    "UNKNOWN",
]

# Same thinness floor as evidence/relation.py's _MIN_PASSAGE_TOKENS -- a
# passage too short to say anything about relation direction is equally
# too short to say anything about statement type.
_MIN_PASSAGE_TOKENS = 4

# ---------------------------------------------------------------------------
# Cue phrase lists. Matched as case-insensitive substrings against the raw
# passage text (lowercased for the English side; Thai text is matched
# as-is since these are short, fairly distinctive multi-character phrases
# and Thai script carries no case). Substring matching on a whole phrase
# (not single tokens) is deliberate: it is precisely the cue *phrase*, not
# any one word in it, that carries the signal, and matching against the
# raw string avoids depending on tokenizer/stemming choices for multi-word
# markers.
# ---------------------------------------------------------------------------

_CONCLUSION_CUES = (
    "in conclusion",
    "we conclude that",
    "this suggests that",
    "overall, our findings",
    "in summary",
    "taken together, these results",
    "these findings suggest that",
    "we conclude",
    # Thai
    "สรุปได้ว่า",
    "โดยสรุป",
)

_RESULT_CUES = (
    "we found",
    "results showed",
    "results indicate",
    "findings reveal",
    "data showed",
    "analysis revealed",
    "the results show",
    "our findings show",
    "results suggest that",
    "results demonstrated",
    "findings indicate",
    "the study found",
    # Thai
    "ผลการศึกษาพบว่า",
    "ผลการวิจัยแสดงให้เห็นว่า",
    "ผลลัพธ์ชี้ให้เห็นว่า",
)

_HYPOTHESIS_CUES = (
    "we hypothesize",
    "the hypothesis is",
    "we discuss the hypothesis that",
    "it is hypothesized that",
    "hypothesized that",
    "we propose the hypothesis",
    "our hypothesis is that",
    # Thai
    "สมมติฐานคือ",
    "ตั้งสมมติฐานว่า",
)

_OBJECTIVE_CUES = (
    "examined whether",
    "the objective was to",
    "aimed to investigate",
    "this study aims to",
    "we investigate whether",
    "this study examined",
    "objective of this study",
    "the purpose of this study is",
    "this paper investigates whether",
    "we aim to examine",
    # Thai
    "มีวัตถุประสงค์เพื่อ",
    "ศึกษาว่า",
    "มุ่งศึกษา",
)

_PRIOR_WORK_CUES = (
    "previous studies suggest",
    "prior literature claims",
    "earlier research found",
    "previous research has shown",
    "prior studies have shown",
    "earlier studies suggest",
    # Thai
    "งานวิจัยก่อนหน้านี้พบว่า",
    "จากการศึกษาที่ผ่านมา",
)
# "according to [X] (19|20)\d\d" -- a bibliographic prior-work citation
# pattern, matched separately since it is a regex, not a fixed phrase.
_PRIOR_WORK_YEAR_RE = re.compile(r"according to [^.;]{1,80}(19|20)\d{2}", re.IGNORECASE)

# Generalized reporting-verb pattern for RESULT, alongside the fixed
# phrase list above: "this trial found", "the study found", "our
# analysis found" -- a finding was reported, regardless of exactly which
# noun names the study. Deliberately scoped to a short, closed set of
# study-naming nouns immediately before "found" (not a bare "found"
# anywhere in the text, which would over-match e.g. a METHOD sentence
# describing how participants were "found"/recruited).
_RESULT_FOUND_RE = re.compile(
    r"\b(this|the|our)\s+(study|trial|research|analysis|paper|investigation)\s+found\b",
    re.IGNORECASE,
)

_LIMITATION_CUES = (
    "a limitation of this study",
    "future research should",
    "one limitation is",
    "limitations of this study include",
    "a key limitation",
    # Thai
    "ข้อจำกัดของการศึกษานี้คือ",
)

_METHOD_CUES = (
    "we used",
    "data were collected via",
    "the sample consisted of",
    "we employed",
    "data was collected through",
    "participants were recruited",
    "data were collected using",
    # Thai
    "ใช้วิธีการ",
    "เก็บข้อมูลโดย",
)


def _contains_any(text: str, cues: tuple[str, ...]) -> bool:
    return any(cue in text for cue in cues)


# ---------------------------------------------------------------------------
# Suppression guard for RESULT/CONCLUSION cues (fixed 2026-09-20, round 4
# adversarial review): a bare substring match on a RESULT/CONCLUSION cue
# phrase is not enough -- the SAME cue phrase can appear inside a
# hypothetical/interrogative subordinate clause ("We aimed to test whether
# the results show that X" -- the cue is quoted, not asserted) or under an
# explicit negation of the report itself ("It is not true that we found
# evidence that X" -- the finding is being denied, not reported). Both are
# confirmed-live false positives for the original cue-matching-only logic:
# they reproduce the exact ABOUT(claim) != EVIDENCE_FOR(claim) category
# error this whole module exists to close, just relocated into the cue
# matcher's own blind spot. This guard is a bounded, sentence-local
# heuristic, not real clause parsing -- it does not catch every case, only
# the two confirmed failure modes above.
# ---------------------------------------------------------------------------

_HYPOTHETICAL_MARKERS = ("whether", " if ")
_HYPOTHETICAL_MARKERS_TH = ("หรือไม่", "หรือเปล่า")

_REPORT_NEGATION_PHRASES = (
    "not true that",
    "not the case that",
    "did not find",
    "does not find",
    "never found",
    "fails to show",
    "failed to find",
    "no evidence that",
    "not found that",
    "did not show",
)
_REPORT_NEGATION_PHRASES_TH = (
    "ไม่เป็นความจริงที่ว่า",
    "ไม่พบว่า",
    "ไม่ได้พบว่า",
)


def _sentence_prefix(text: str, pos: int) -> str:
    """The portion of `text` from the start of the sentence containing
    `pos` up to (not including) `pos` itself -- what a reader would have
    seen before reaching a cue match at that position.
    """
    start = max(text.rfind(".", 0, pos), text.rfind("!", 0, pos), text.rfind("?", 0, pos))
    start = start + 1 if start >= 0 else 0
    return text[start:pos]


def _is_suppressed_finding_cue(text: str, match_start: int) -> bool:
    """True if a RESULT/CONCLUSION cue match at `match_start` sits inside a
    hypothetical/interrogative framing (a "whether"/"if" clause earlier in
    the same sentence) or is itself negated (an explicit "we did NOT find"/
    "it is not true that we found" phrase earlier in the same sentence) --
    in either case the cue does not actually assert a reported finding.
    """
    preceding = _sentence_prefix(text, match_start)
    if any(m in preceding for m in _HYPOTHETICAL_MARKERS):
        return True
    if any(m in preceding for m in _HYPOTHETICAL_MARKERS_TH):
        return True
    if any(m in preceding for m in _REPORT_NEGATION_PHRASES):
        return True
    if any(m in preceding for m in _REPORT_NEGATION_PHRASES_TH):
        return True
    return False


def _first_unsuppressed_cue(text: str, cues: tuple[str, ...]) -> bool:
    """Like `_contains_any`, but a match is only accepted if it is not
    suppressed by `_is_suppressed_finding_cue` -- used for RESULT/
    CONCLUSION cues, where a false positive can lead to a false ADMIT.
    """
    for cue in cues:
        idx = text.find(cue)
        if idx != -1 and not _is_suppressed_finding_cue(text, idx):
            return True
    return False


def _unsuppressed_regex_match(text: str, pattern: re.Pattern[str]) -> bool:
    for m in pattern.finditer(text):
        if not _is_suppressed_finding_cue(text, m.start()):
            return True
    return False


def classify_statement_type(passage: str) -> StatementType:
    """Deterministic v1 heuristic: passage -> `StatementType`.

    Precedence when a passage matches cues from more than one tier
    (e.g. an OBJECTIVE clause in one sentence and a RESULT clause in
    another) -- documented explicitly, strongest/most-specific-first:

        RESULT / CONCLUSION
            > HYPOTHESIS / OBJECTIVE / PRIOR_WORK
            > METHOD / LIMITATION
            > BACKGROUND
            > UNKNOWN

    Within the top tier, CONCLUSION is checked before RESULT: a
    conclusion cue ("in conclusion", "we conclude that", ...) is the more
    synthesized, more specific claim of the two when both appear (a
    conclusion sentence often restates a result), so it is preferred.
    Within the second tier, HYPOTHESIS is checked before OBJECTIVE before
    PRIOR_WORK -- a hypothesis marker is the most specific/rare surface
    form of the three, an objective/aim statement the next most specific,
    and a prior-work citation the most generic framing of the three.
    Within the third tier, LIMITATION is checked before METHOD -- a
    limitation statement is the more specific, less common phrasing.

    Returns "UNKNOWN" when the passage is empty or too thin (same
    `_MIN_PASSAGE_TOKENS` floor as `evidence/relation.py`) to say
    anything at all -- not a guess in either direction.

    Returns "BACKGROUND" for a passage with real topical content but none
    of the recognized cue phrases -- generic scene-setting.
    """
    if not passage or not passage.strip():
        return "UNKNOWN"

    tokens = _shared_tokenize(passage)
    if len(tokens) < _MIN_PASSAGE_TOKENS:
        return "UNKNOWN"

    text = passage.lower()

    # Tier 1 -- an actual finding was reported. A cue match here is only
    # trusted if it is not sitting inside a hypothetical/interrogative
    # clause or under an explicit negation of the report itself -- see
    # `_is_suppressed_finding_cue`'s docstring for the two confirmed
    # failure modes this guards against.
    if _first_unsuppressed_cue(text, _CONCLUSION_CUES):
        return "CONCLUSION"
    if _first_unsuppressed_cue(text, _RESULT_CUES) or _unsuppressed_regex_match(
        text, _RESULT_FOUND_RE
    ):
        return "RESULT"

    # Tier 2 -- framing that discusses/examines the claim, no finding yet.
    if _contains_any(text, _HYPOTHESIS_CUES):
        return "HYPOTHESIS"
    if _contains_any(text, _OBJECTIVE_CUES):
        return "OBJECTIVE"
    if _contains_any(text, _PRIOR_WORK_CUES) or _PRIOR_WORK_YEAR_RE.search(text):
        return "PRIOR_WORK"

    # Tier 3 -- procedural/caveat framing, no finding about the claim.
    if _contains_any(text, _LIMITATION_CUES):
        return "LIMITATION"
    if _contains_any(text, _METHOD_CUES):
        return "METHOD"

    # Tier 4 -- real topical content, no recognized cue at all.
    return "BACKGROUND"


# Statement types that report an actual finding -- the only types that may
# ever lead to ADMIT in `evidence/verifier.py::gate_admission_decision()`.
FINDING_STATEMENT_TYPES: frozenset[StatementType] = frozenset({"RESULT", "CONCLUSION"})

# Every legal `StatementType` value (mirrors the `Literal` above at runtime).
# Used by `evidence/verifier.py::check_claim_evidence()` to silently reject
# an AI-Reader-proposed `ai_statement_type` that is not a real StatementType
# value, per the AI Discovery Contract (an AI proposal is untrusted input,
# never trusted or crashed on -- see that function's docstring).
ALL_STATEMENT_TYPES: frozenset[StatementType] = frozenset(
    {
        "BACKGROUND",
        "OBJECTIVE",
        "HYPOTHESIS",
        "METHOD",
        "RESULT",
        "CONCLUSION",
        "LIMITATION",
        "PRIOR_WORK",
        "UNKNOWN",
    }
)
