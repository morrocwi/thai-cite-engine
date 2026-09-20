"""Regression test: no literal email address anywhere in `src/`.

Encodes the founder's privacy requirement directly (per the adapter
docstrings: `THAICITE_CONTACT_EMAIL` / `THAICITE_NCBI_API_KEY` are read
from the environment at call time and never hardcoded into source) as an
automated check, not a one-time manual grep. Fails loudly with the exact
file/line if a literal email-shaped string is ever committed to `src/`.
"""

from __future__ import annotations

import re
from pathlib import Path

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


def _source_files():
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def test_no_literal_email_address_anywhere_in_src():
    offenders: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = _EMAIL_RE.search(line)
            if match:
                offenders.append(f"{path.relative_to(_SRC_ROOT.parent)}:{lineno}: {match.group(0)!r}")

    assert not offenders, (
        "Found literal email address(es) hardcoded in src/ -- contact "
        "emails must only ever be read from environment variables "
        "(THAICITE_CONTACT_EMAIL, THAICITE_NCBI_API_KEY), never hardcoded:\n"
        + "\n".join(offenders)
    )


def test_src_has_at_least_one_python_file_so_this_check_is_not_vacuous():
    assert list(_source_files()), "src/ contains no .py files -- the email-leak scan found nothing to scan."
