"""Shared alphanumeric tokenizer -- Thai-aware, English-unchanged.

Both `evidence/verifier.py` (`_keywords`) and `evidence/relation.py`
(`_tokenize`) used to call the same naive regex directly:

    re.findall(r"[a-zA-Z0-9฀-๿]+", text.lower())

That regex only breaks a token on a non-alphanumeric character (whitespace,
punctuation). English text has inter-word spaces, so this works fine for
English. Thai script does NOT use inter-word spaces -- a whole Thai sentence
or title, if it contains no punctuation/digits, becomes exactly ONE token
under this regex. Any token-SET overlap comparison (title vs. query,
passage vs. claim) then sees zero shared tokens between two genuinely
matching Thai strings, because each string was reduced to one giant
"word" that can never equal the other string's giant "word".

This module provides one shared `tokenize(text)` helper that:

  - Detects whether `text` contains Thai-script characters
    (U+0E00-U+0E7F, `_THAI_SCRIPT_RE`).
  - For Thai (or mixed Thai+English) text: runs `pythainlp.tokenize.
    word_tokenize` to get real word boundaries, then applies the SAME
    alphanumeric-filtering discipline the old regex applied per-piece
    (each resulting word is itself re-scanned with the old regex and
    lowercased), so punctuation/whitespace tokens pythainlp may emit are
    dropped exactly like before, and any embedded Latin/digit runs are
    still tokenized consistently with the non-Thai path.
  - For pure non-Thai text: returns EXACTLY what the old regex returned
    (same order, same tokens, same lowercasing) -- this is the "do not
    change English-only matching" requirement, verified by
    tests/test_g6_fix_offline.py's existing English fixtures (Watson &
    Crick / Vaswani / ResNet) continuing to pass unchanged.
  - Falls back to the OLD regex behavior for Thai text too, if pythainlp
    is unavailable or raises, via a light try/except around the import
    and the call. This is a documented, deliberate degrade: Thai
    token-overlap recall reverts to the pre-fix (broken) behavior in that
    case, but the pipeline never crashes. `tokenize()` does not silently
    pretend segmentation happened -- callers that care can check
    `PYTHAINLP_AVAILABLE` (computed once at import time) or catch the
    fact that Thai strings come back as one giant token again.
"""

from __future__ import annotations

import re

# Thai script block (consonants, vowels, tone marks, digits): U+0E00-U+0E7F.
_THAI_SCRIPT_RE = re.compile(r"[฀-๿]")

# The original naive tokenizer -- still used verbatim for pure non-Thai
# text, and reused piecewise to filter/normalize each pythainlp word on the
# Thai path (see module docstring).
_ALNUM_RE = re.compile(r"[a-zA-Z0-9฀-๿]+")

try:  # pragma: no cover - exercised indirectly; import cost is real but small.
    from pythainlp.tokenize import word_tokenize as _pythainlp_word_tokenize

    PYTHAINLP_AVAILABLE = True
except Exception:  # noqa: BLE001 - any import-time failure degrades gracefully.
    _pythainlp_word_tokenize = None
    PYTHAINLP_AVAILABLE = False


def contains_thai(text: str) -> bool:
    """True iff `text` contains at least one Thai-script character."""
    return bool(text) and bool(_THAI_SCRIPT_RE.search(text))


def _regex_tokenize(text: str) -> list[str]:
    """The original naive tokenizer, unchanged: lowercase, then
    `[a-zA-Z0-9฀-๿]+` runs. Used directly for non-Thai text, and as the
    documented fallback for Thai text when pythainlp is unavailable/fails.
    """
    return _ALNUM_RE.findall((text or "").lower())


def _thai_tokenize(text: str) -> list[str]:
    """Real Thai word segmentation via pythainlp, with the same
    alphanumeric-filtering/lowercasing discipline as `_regex_tokenize`
    applied to each resulting word (so stray punctuation/whitespace
    tokens pythainlp may emit are dropped, exactly like the old regex
    dropped them, and any Latin/digit sub-runs stay consistent with the
    non-Thai path).
    """
    try:
        words = _pythainlp_word_tokenize(text, engine="newmm")
    except Exception:  # noqa: BLE001 - degrade, never crash on a bad segment.
        return _regex_tokenize(text)

    tokens: list[str] = []
    for word in words:
        tokens.extend(_ALNUM_RE.findall(word.lower()))
    return tokens


def tokenize(text: str) -> list[str]:
    """Tokenize `text` into lowercase alphanumeric (incl. Thai-script)
    tokens, using real Thai word segmentation when the text contains Thai
    script and pythainlp is available, and the original regex behavior
    otherwise (pure non-Thai text, or Thai text with pythainlp
    unavailable/failing -- see module docstring for the documented
    fallback).
    """
    if not text:
        return []
    if contains_thai(text) and PYTHAINLP_AVAILABLE:
        return _thai_tokenize(text)
    return _regex_tokenize(text)
