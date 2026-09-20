"""Thai-relevance tagging: classification/metadata only, never a gate.

Described in prose in ARCHITECTURE.md §4/§107; this module is the first real
implementation. Given a source record (a real `Candidate` -- anything with
`.title`, `.abstract`, `.url`, `.raw_metadata`, `.issn`), classify it against
four NON-EXCLUSIVE levels (a record can carry more than one tag, or none):

  THAI_LANGUAGE          -- title/abstract text is substantially in Thai
                             script.
  PUBLISHED_IN_THAILAND  -- journal/institution/host is Thai (a ThaiJO-style
                             host, a .ac.th/.go.th/.or.th domain in the
                             record's own source metadata, a known Thai
                             university/publisher name, or an author
                             institution OpenAlex itself tags country_code
                             "TH").
  ABOUT_THAILAND         -- title/abstract mentions Thailand, a Thai
                             region/province, or a clearly Thailand-specific
                             population (keyword-list based, adequate for
                             this prototype).
  FOREIGN_ABOUT_THAILAND -- ABOUT_THAILAND is true but THAI_LANGUAGE and
                             PUBLISHED_IN_THAILAND are both false, i.e.
                             non-Thai authors/venue studying Thailand.

IMPORTANT: this is classification/tagging only. It must NEVER be used to
admit or reject a citation on its own -- it is metadata attached to
Candidate.thai_relevance / Citation.thai_relevance for the caller, not a new
gate. See evidence/verifier.py for the actual G1-G7 gates.
"""

from __future__ import annotations

import re
from typing import Any

THAI_LANGUAGE = "THAI_LANGUAGE"
PUBLISHED_IN_THAILAND = "PUBLISHED_IN_THAILAND"
ABOUT_THAILAND = "ABOUT_THAILAND"
FOREIGN_ABOUT_THAILAND = "FOREIGN_ABOUT_THAILAND"

# Thai script block (consonants, vowels, tone marks, digits): U+0E00-U+0E7F.
_THAI_SCRIPT_RE = re.compile(r"[฀-๿]")

# A record is treated as substantially Thai-language once at least this
# fraction of its alphabetic characters are Thai script. Deliberately not
# "any Thai character at all" -- a mostly-English abstract with one Thai
# proper noun should not be tagged THAI_LANGUAGE.
_THAI_LANGUAGE_THRESHOLD = 0.3

# "Thailand", "Thai", major regions/provinces commonly studied in the
# literature. Keyword-list based, per the task spec -- adequate for this
# prototype, not exhaustive.
_THAILAND_KEYWORDS = {
    "thailand", "thai", "bangkok", "siam", "siamese",
    "chiang mai", "chiangmai", "chiang rai", "chiangrai",
    "chonburi", "chon buri", "phuket", "khon kaen", "khonkaen",
    "nakhon ratchasima", "korat", "udon thani", "udonthani",
    "ubon ratchathani", "songkhla", "hat yai", "hatyai",
    "nonthaburi", "pattaya", "ayutthaya", "ayudhya",
    "isan", "isaan", "northern thailand", "southern thailand",
    "northeastern thailand", "mekong", "krabi", "surat thani",
    "rayong", "lampang", "mae hong son", "nakhon si thammarat",
    "trang", "pattani", "yala", "narathiwat", "sukhothai",
    "kanchanaburi", "prachuap khiri khan", "ratchaburi",
    "samut prakan", "samut sakhon", "nakhon pathom", "phitsanulok",
    "loei", "phetchabun", "buriram", "buri ram", "surin", "sisaket",
    "roi et", "mukdahan", "nakhon phanom", "kalasin", "nong khai",
}

# Substrings/patterns that indicate a Thai host/journal/publisher when found
# in a record's own URL, landing page, or host-organization metadata.
_THAI_HOST_PATTERNS = (".ac.th", ".go.th", ".or.th", ".co.th", "thaijo.org")

# Well-known Thai university/publisher names, lowercase, for a
# host-organization-name / institution-name match.
_THAI_PUBLISHER_KEYWORDS = {
    "chulalongkorn", "mahidol", "thammasat", "chiang mai university",
    "kasetsart", "khon kaen university", "prince of songkla",
    "prince of songkhla", "thailand ministry",
    "national research council of thailand", "thaijo",
    "srinakharinwirot", "silpakorn", "burapha", "ramkhamhaeng",
    "king mongkut", "suranaree", "walailak", "naresuan",
    "thailand science research and innovation",
}


def _is_thai_language(text: str) -> bool:
    if not text:
        return False
    thai_chars = len(_THAI_SCRIPT_RE.findall(text))
    letters = sum(1 for ch in text if ch.isalpha())
    if letters == 0:
        return False
    return (thai_chars / letters) >= _THAI_LANGUAGE_THRESHOLD


def _mentions_thailand(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(keyword in lowered for keyword in _THAILAND_KEYWORDS)


def _published_in_thailand(candidate: Any) -> bool:
    """Journal/institution/host signal for `candidate`, read defensively.

    Never assumes a particular adapter's raw_metadata shape beyond
    `dict.get` -- an adapter that doesn't expose this metadata simply
    yields no PUBLISHED_IN_THAILAND signal, it never raises.
    """
    fields: list[str] = []

    url = getattr(candidate, "url", None)
    if url:
        fields.append(str(url).lower())

    raw = getattr(candidate, "raw_metadata", None)
    if isinstance(raw, dict):
        primary_location = raw.get("primary_location")
        if isinstance(primary_location, dict):
            landing = primary_location.get("landing_page_url")
            if landing:
                fields.append(str(landing).lower())
            source = primary_location.get("source")
            if isinstance(source, dict):
                host_org_name = source.get("host_organization_name")
                if host_org_name:
                    fields.append(str(host_org_name).lower())
                display_name = source.get("display_name")
                if display_name:
                    fields.append(str(display_name).lower())

        for authorship in raw.get("authorships") or []:
            if not isinstance(authorship, dict):
                continue
            for institution in authorship.get("institutions") or []:
                if not isinstance(institution, dict):
                    continue
                country_code = institution.get("country_code")
                if country_code and str(country_code).upper() == "TH":
                    return True
                name = institution.get("display_name")
                if name:
                    fields.append(str(name).lower())

    blob = " ".join(fields)
    if any(pattern in blob for pattern in _THAI_HOST_PATTERNS):
        return True
    if any(keyword in blob for keyword in _THAI_PUBLISHER_KEYWORDS):
        return True
    return False


def classify_thai_relevance(candidate: Any) -> list[str]:
    """Return the non-exclusive list of Thai-relevance tags for `candidate`.

    `candidate` needs `.title`, `.abstract`, `.url`, `.raw_metadata`
    attributes -- a real `Candidate` (core/models.py) satisfies this.
    Classification only: never used to admit/reject a citation.
    """
    title = getattr(candidate, "title", None) or ""
    abstract = getattr(candidate, "abstract", None) or ""
    text = f"{title} {abstract}"

    is_thai_language = _is_thai_language(text)
    is_published_in_thailand = _published_in_thailand(candidate)
    is_about_thailand = _mentions_thailand(text)

    tags: list[str] = []
    if is_thai_language:
        tags.append(THAI_LANGUAGE)
    if is_published_in_thailand:
        tags.append(PUBLISHED_IN_THAILAND)
    if is_about_thailand:
        tags.append(ABOUT_THAILAND)
    if is_about_thailand and not is_thai_language and not is_published_in_thailand:
        tags.append(FOREIGN_ABOUT_THAILAND)

    return tags
