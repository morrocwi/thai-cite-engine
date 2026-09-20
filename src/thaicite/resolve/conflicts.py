"""Conflict detection: two sources disagreeing on a field must never be
silently resolved. Detected conflicts move a CanonicalWork to CONFLICT and
record both assertions with provenance (which candidate/adapter said what).
"""

from __future__ import annotations

from typing import Any

from thaicite.core.models import CanonicalWork, VerificationState
from thaicite.resolve.identity import _normalize_title

# Fields checked for cross-source disagreement once candidates have been
# merged into one CanonicalWork (i.e. they are believed to be the same work
# via an exact identifier or bibliographic match).
_CHECKED_FIELDS = ("title", "year", "authors")


def _values_conflict(field_name: str, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    if field_name == "title":
        return _normalize_title(a) != _normalize_title(b)
    if field_name == "authors":
        # Compare first-author surname only; full author-list ordering
        # differences across sources are common and not a real conflict.
        fa = (a[0].strip().lower() if a else None)
        fb = (b[0].strip().lower() if b else None)
        return bool(fa and fb and fa != fb)
    return a != b


def detect_conflicts(work: CanonicalWork) -> list[dict[str, Any]]:
    """Return a list of conflict records found across work.candidates.

    Each record: {"field", "assertions": [{"adapter", "record_id", "value"}]}.
    Does not mutate `work`; see `apply_conflict_state` for that.
    """
    if len(work.candidates) < 2:
        return []

    conflicts: list[dict[str, Any]] = []
    baseline = work.candidates[0]
    for field_name in _CHECKED_FIELDS:
        base_value = getattr(baseline, field_name)
        disagreeing = []
        for c in work.candidates[1:]:
            value = getattr(c, field_name)
            if _values_conflict(field_name, base_value, value):
                disagreeing.append(c)
        if disagreeing:
            assertions = [
                {
                    "adapter": baseline.source_adapter,
                    "record_id": baseline.source_record_id,
                    "value": base_value,
                }
            ] + [
                {
                    "adapter": c.source_adapter,
                    "record_id": c.source_record_id,
                    "value": getattr(c, field_name),
                }
                for c in disagreeing
            ]
            conflicts.append({"field": field_name, "assertions": assertions})
    return conflicts


def apply_conflict_state(work: CanonicalWork) -> CanonicalWork:
    """Detect conflicts on `work` and, if any exist, move it to CONFLICT.

    Never silently picks a value — both/all conflicting assertions stay
    recorded in `work.conflicts` with provenance. A work in CONFLICT is
    blocked from VERIFIED by the evidence gate (G7) until a human or a
    later, non-silent resolution step clears it.
    """
    found = detect_conflicts(work)
    if found:
        work.conflicts = found
        work.state = VerificationState.CONFLICT
    return work
