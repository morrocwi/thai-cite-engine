"""Abstract adapter interface.

`search(query)` must return either a list of `RawRecord` (each carrying a
`source_record_id`, raw metadata, and a retrieval-timestamp placeholder) or
an explicit `AdapterError` tagged with one of the distinct failure states.
A real transport error (HTTP timeout, 5xx, connection refused, malformed
response) must NEVER surface as NOT_FOUND -- NOT_FOUND is reserved for a
genuine, successfully-executed empty result set.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from thaicite.core.models import VerificationState

# The only failure tags an adapter may return via AdapterError.state.
_ADAPTER_ERROR_STATES = frozenset(
    {
        VerificationState.NOT_FOUND,
        VerificationState.RATE_LIMITED,
        VerificationState.TIMEOUT,
        VerificationState.ACCESS_DENIED,
        VerificationState.PARSER_ERROR,
    }
)


@dataclass
class RawRecord:
    """One untyped hit from an adapter's search, before it becomes a Candidate."""

    source_record_id: str
    raw_metadata: dict[str, Any]
    retrieved_at: float = field(default_factory=time.time)


@dataclass
class AdapterError:
    """An explicit, tagged failure from an adapter search.

    `state` must be one of the distinct failure states (never a bare
    string chosen ad hoc) so callers can branch on it reliably and so a
    real error can never be laundered into "not found".
    """

    state: str
    adapter: str
    message: str
    note: str | None = None
    # Optional structured Coverage Readout detail (core/coverage.py) an
    # adapter can attach to its own error -- e.g. ThaiJOAdapter attaches
    # per-endpoint harvest status + index-staleness info here so a NOT_FOUND
    # can be told apart from "this source was never actually searched" (see
    # core/coverage.py's module docstring). `None` means the adapter has no
    # sub-endpoint/coverage detail to add beyond `state`/`message`.
    coverage: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.state not in _ADAPTER_ERROR_STATES:
            raise ValueError(
                f"AdapterError.state must be one of {sorted(_ADAPTER_ERROR_STATES)}, "
                f"got {self.state!r}"
            )
        if self.state == VerificationState.NOT_FOUND and not self.note:
            # Force the "not found via this adapter, not does-not-exist"
            # distinction to be explicit at the call site rather than
            # implied.
            self.note = (
                "No records returned by this adapter for this query -- this "
                "means 'not found via this adapter', not 'this work does not "
                "exist'."
            )


class SourceAdapter(ABC):
    """Base class every real adapter (OpenAlex, Crossref, ...) implements."""

    name: str = "BASE"

    @abstractmethod
    def search(self, query: str) -> list[RawRecord] | AdapterError:
        """Search the external source. Returns records or a tagged error.

        Implementations MUST NOT catch a transport-level failure and
        return it as an empty list / NOT_FOUND -- that would collapse a
        real error into "no record found", which is exactly the confusion
        this interface exists to prevent.
        """
        raise NotImplementedError

    @abstractmethod
    def to_candidates(self, records: list[RawRecord]) -> list[Any]:
        """Convert this adapter's RawRecords into core.models.Candidate objects.

        Every field must come from `records` (i.e. from the real source
        response) -- an adapter must never fabricate a field here.
        """
        raise NotImplementedError
