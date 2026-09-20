"""Identity resolution: merge Candidates into CanonicalWorks.

Merging is allowed ONLY via an exact persistent identifier match (DOI, PMID,
PMCID, arXiv id) or an exact bibliographic match (ISSN + normalized title +
year + first-author surname). Semantic/embedding/string similarity may be
computed and attached to a Candidate for RANKING ONLY — it is a hard,
testable boundary that `can_merge()` never returns True on similarity alone,
no matter how high the score.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from thaicite.core.models import Candidate, CanonicalWork, VerificationState

# Identifier fields checked for exact-match merging, in priority order.
_EXACT_ID_FIELDS = ("doi", "pmid", "pmcid", "arxiv_id")


def _normalize_id(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip().lower()
    v = v.removeprefix("https://doi.org/")
    v = v.removeprefix("doi:")
    v = v.removeprefix("pmid:")
    v = v.removeprefix("pmcid:")
    v = v.removeprefix("arxiv:")
    return v or None


def _normalize_title(title: str | None) -> str | None:
    """Normalize a title for exact-match comparison.

    Bug fixed 2026-09-20 (S065, tests/golden/CONCEPT_VALIDATION_REPORT.md):
    `\\w` (used inside a `[^\\w\\s]` "strip punctuation" regex) does NOT
    match Thai combining tone/vowel marks (Unicode category Mn), so the old
    version silently stripped them as if they were punctuation -- e.g.
    'ปัญหาการสื่อสาร' -> 'ปญหาการสอสาร'. Two Thai titles differing only by
    tone/vowel marks are a real semantic difference and must NOT normalize
    identically (this feeds both `_values_conflict('title', ...)` and
    `bibliographic_exact_match`, used for identity merging). Fix: strip only
    characters in Unicode general categories P* (punctuation) and S*
    (symbols) explicitly, so combining marks (Mn/Mc) are always preserved
    regardless of script.
    """
    if not title:
        return None
    t = unicodedata.normalize("NFKC", title).lower().strip()
    t = "".join(
        ch
        for ch in t
        if not unicodedata.category(ch).startswith("P")
        and not unicodedata.category(ch).startswith("S")
    )
    t = re.sub(r"\s+", " ", t)
    return t or None


def _first_author_surname(authors: list[str] | None) -> str | None:
    if not authors:
        return None
    first = authors[0].strip()
    if not first:
        return None
    # Accept "Surname, First" or "First Surname" — take the token that
    # looks like the surname (before a comma, or the last space-token).
    if "," in first:
        surname = first.split(",", 1)[0]
    else:
        parts = first.split()
        surname = parts[-1] if parts else first
    return unicodedata.normalize("NFKC", surname).lower().strip() or None


def exact_identifier_match(a: Candidate, b: Candidate) -> str | None:
    """Return the field name on which a and b match exactly, or None.

    Matching is over normalized identifier values so trivial formatting
    differences (a DOI URL vs a bare DOI) do not block a real match, but
    no fuzziness beyond that normalization is applied.
    """
    for field_name in _EXACT_ID_FIELDS:
        va = _normalize_id(getattr(a, field_name, None))
        vb = _normalize_id(getattr(b, field_name, None))
        if va and vb and va == vb:
            return field_name
    return None


def bibliographic_exact_match(a: Candidate, b: Candidate) -> bool:
    """ISSN + normalized title + year + first-author-surname, all exact."""
    if not (a.issn and b.issn and a.issn.strip() == b.issn.strip()):
        return False
    if a.year is None or b.year is None or a.year != b.year:
        return False
    ta, tb = _normalize_title(a.title), _normalize_title(b.title)
    if not ta or not tb or ta != tb:
        return False
    sa, sb = _first_author_surname(a.authors), _first_author_surname(b.authors)
    if not sa or not sb or sa != sb:
        return False
    return True


def similarity_score(a: Candidate, b: Candidate) -> float:
    """A semantic/string similarity score in [0, 1], for RANKING ONLY.

    This function's return value MUST NEVER be used as sole grounds to
    merge two candidates or to promote a work toward VERIFIED. It is
    computed here (simple normalized-title ratio, adequate for a
    prototype) purely so callers can rank/display candidates; see
    `can_merge()` for the actual merge boundary, which ignores this score
    entirely.
    """
    ta, tb = _normalize_title(a.title) or "", _normalize_title(b.title) or ""
    if not ta or not tb:
        return 0.0
    return SequenceMatcher(None, ta, tb).ratio()


def can_merge(a: Candidate, b: Candidate) -> bool:
    """The explicit, testable merge boundary.

    Returns True ONLY for an exact persistent-identifier match or an exact
    ISSN+title+year+author bibliographic match. A caller MUST NOT pass a
    similarity_score() result into this function's decision, and this
    function does not itself look at similarity_score() at all — so even
    two candidates with similarity 0.99 (or 1.0) return False here unless
    one of the exact criteria above also holds.
    """
    if exact_identifier_match(a, b) is not None:
        return True
    if bibliographic_exact_match(a, b):
        return True
    return False


def resolve_identities(candidates: list[Candidate]) -> list[CanonicalWork]:
    """Group candidates into CanonicalWork clusters using can_merge() only.

    Union-find style clustering: two candidates land in the same cluster
    iff can_merge() says so (directly, or transitively through a chain of
    exact matches). Candidates that merge with nothing form singleton
    CanonicalWorks in DISCOVERED state (they still have one real source
    record — just not corroborated by another one yet).
    """
    n = len(candidates)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):
        for j in range(i + 1, n):
            if can_merge(candidates[i], candidates[j]):
                union(i, j)

    clusters: dict[int, list[Candidate]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(candidates[i])

    works: list[CanonicalWork] = []
    for members in clusters.values():
        merged_ids: dict[str, str] = {}
        for field_name in _EXACT_ID_FIELDS:
            for c in members:
                v = _normalize_id(getattr(c, field_name, None))
                if v:
                    merged_ids.setdefault(field_name, v)
        state = (
            VerificationState.IDENTIFIED
            if len(members) > 1 or merged_ids
            else VerificationState.DISCOVERED
        )
        works.append(
            CanonicalWork(candidates=members, state=state, merged_identifiers=merged_ids)
        )
    return works
