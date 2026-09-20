"""Offline, unit-level revalidation of the G6 fix (evidence/verifier.py).

Purpose (2026-09-20 revalidation run): tests/golden/CONCEPT_VALIDATION_REPORT.md
documented POSITIVE_CONTROL 15/15 FAIL against the OLD G6 (free-text `context`
vs. candidate keyword overlap, no candidate-vs-query identity check at all).
`src/thaicite/evidence/verifier.py` now carries a NEW `gate_g6_identity_match`
that compares each candidate's own title/authors against the QUERY CITATION
STRING. This file re-proves that fix WITHOUT touching the live, rate-limited
OpenAlex API: every fixture below is a hand-authored, OpenAlex-response-shaped
JSON object (same field names/shape the real API returns -- `id`,
`display_name`/`title`, `publication_year`, `authorships`, `doi`,
`primary_location`, `abstract_inverted_index`), run through the REAL,
unmocked `OpenAlexAdapter.to_candidates()` conversion (a pure function, no
network call) and then the REAL, unmocked G6 gate logic in `verify()`.

Nothing here mocks or stubs `gate_g6_identity_match`, `verify`, or
`classify_thai_relevance` -- these are the actual production functions,
exercised with synthetic input instead of a live HTTP round-trip.
"""

from __future__ import annotations

from thaicite.adapters.base import RawRecord
from thaicite.adapters.openalex import OpenAlexAdapter
from thaicite.core.models import CanonicalWork, VerificationState
from thaicite.evidence.verifier import verify
from thaicite.normalize.thai_relevance import (
    ABOUT_THAILAND,
    FOREIGN_ABOUT_THAILAND,
    PUBLISHED_IN_THAILAND,
    THAI_LANGUAGE,
    classify_thai_relevance,
)

_ADAPTER = OpenAlexAdapter()


def _inverted_index(text: str) -> dict[str, list[int]]:
    """Build an OpenAlex-style abstract_inverted_index from plain text.

    Real OpenAlex responses serve abstracts this way; this reproduces that
    exact shape so `_reconstruct_abstract()` in the real adapter code runs
    unmodified against it.
    """
    index: dict[str, list[int]] = {}
    for pos, word in enumerate(text.split()):
        index.setdefault(word, []).append(pos)
    return index


def _openalex_work(
    *,
    work_id: str,
    title: str,
    authors: list[str],
    year: int,
    doi: str,
    abstract: str,
    country_codes: list[str] | None = None,
    host_org: str | None = None,
    landing_page: str | None = None,
) -> dict:
    """A synthetic OpenAlex `works` API item, real field names/shape."""
    country_codes = country_codes or ["US"]
    return {
        "id": work_id,
        "title": title,
        "display_name": title,
        "publication_year": year,
        "doi": f"https://doi.org/{doi}",
        "authorships": [
            {
                "author": {"display_name": name},
                "institutions": [
                    {
                        "display_name": "Example Institute",
                        "country_code": country_codes[i % len(country_codes)],
                    }
                ],
            }
            for i, name in enumerate(authors)
        ],
        "primary_location": {
            "landing_page_url": landing_page or f"https://example.org/{work_id}",
            "source": {
                "display_name": "Example Venue",
                "host_organization_name": host_org,
                "issn": ["1234-5678"],
            },
        },
        "abstract_inverted_index": _inverted_index(abstract),
    }


def _candidate_from_work(work_json: dict):
    record = RawRecord(source_record_id=work_json["id"], raw_metadata=work_json)
    candidates = _ADAPTER.to_candidates([record])
    assert len(candidates) == 1, "one synthetic work must yield exactly one Candidate"
    return candidates[0]


def _verify_single(work_json: dict, query: str, context: str = ""):
    candidate = _candidate_from_work(work_json)
    work = CanonicalWork(candidates=[candidate])
    return verify(work, context=context, query=query)


# ---------------------------------------------------------------------------
# Case 1 (task item 1, primary case): Vaswani et al. 2017, "Attention Is All
# You Need" -- POSITIVE_CONTROL S018 in the report. Real paper must now be
# VERIFIED; SegFormer/BEiT/Swin-style distractors sharing only
# "transformer"/"attention" must now be REJECTED at G6.
# ---------------------------------------------------------------------------

_VASWANI_QUERY = "Vaswani et al. (2017), Attention Is All You Need, NeurIPS"

_VASWANI_WORK = _openalex_work(
    work_id="https://openalex.org/W2963403868",
    title="Attention Is All You Need",
    authors=["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"],
    year=2017,
    doi="10.5555/3295222.3295349",
    abstract=(
        "We propose a new simple network architecture the Transformer based "
        "solely on attention mechanisms dispensing with recurrence and "
        "convolutions entirely"
    ),
)

_SEGFORMER_WORK = _openalex_work(
    work_id="https://openalex.org/W3211111111",
    title="SegFormer: Simple and Efficient Design for Semantic Segmentation with Transformers",
    authors=["Enze Xie", "Wenhai Wang"],
    year=2021,
    doi="10.5555/segformer",
    abstract="A simple efficient yet powerful semantic segmentation framework",
)

_BEIT_WORK = _openalex_work(
    work_id="https://openalex.org/W3222222222",
    title="BEiT: BERT Pre-Training of Image Transformers",
    authors=["Hangbo Bao", "Li Dong"],
    year=2021,
    doi="10.5555/beit",
    abstract="A self-supervised vision representation model",
)

_SWIN_WORK = _openalex_work(
    work_id="https://openalex.org/W3233333333",
    title="Swin Transformer: Hierarchical Vision Transformer using Shifted Windows",
    authors=["Ze Liu", "Yutong Lin"],
    year=2021,
    doi="10.5555/swin",
    abstract="A hierarchical Transformer whose representation is computed with shifted windows",
)


def test_vaswani_real_paper_now_verified():
    work, _ = _verify_single(_VASWANI_WORK, query=_VASWANI_QUERY)
    assert work.state == VerificationState.VERIFIED, work.gate_results
    assert work.gate_results["G6_identity_match"] is True


def test_vaswani_segformer_distractor_now_rejected():
    work, _ = _verify_single(_SEGFORMER_WORK, query=_VASWANI_QUERY)
    assert work.state == VerificationState.REJECTED, work.gate_results
    assert work.gate_results["G6_identity_match"] is False


def test_vaswani_beit_distractor_now_rejected():
    work, _ = _verify_single(_BEIT_WORK, query=_VASWANI_QUERY)
    assert work.state == VerificationState.REJECTED, work.gate_results
    assert work.gate_results["G6_identity_match"] is False


def test_vaswani_swin_distractor_now_rejected():
    work, _ = _verify_single(_SWIN_WORK, query=_VASWANI_QUERY)
    assert work.state == VerificationState.REJECTED, work.gate_results
    assert work.gate_results["G6_identity_match"] is False


# ---------------------------------------------------------------------------
# Case 2 (task item 1, second POSITIVE_CONTROL): Watson & Crick 1953 vs.
# unrelated DNA/nucleic-acid papers -- POSITIVE_CONTROL S016 in the report.
# ---------------------------------------------------------------------------

_WATSON_CRICK_QUERY = (
    "Watson JD, Crick FH (1953). Molecular structure of nucleic acids: "
    "a structure for deoxyribose nucleic acid. Nature."
)

_WATSON_CRICK_WORK = _openalex_work(
    work_id="https://openalex.org/W2013407343",
    title="Molecular structure of nucleic acids: a structure for deoxyribose nucleic acid",
    authors=["J. D. Watson", "F. H. C. Crick"],
    year=1953,
    doi="10.1038/171737a0",
    abstract="A structure for deoxyribose nucleic acid has been proposed",
)

_DNA_DISTRACTOR_1 = _openalex_work(
    work_id="https://openalex.org/W4011111111",
    title="Synthesis of modified oligonucleotides for antisense therapy applications",
    authors=["A. Researcher", "B. Scientist"],
    year=2015,
    doi="10.5555/dna1",
    abstract="Modified oligonucleotides show improved stability against nucleases",
)

_DNA_DISTRACTOR_2 = _openalex_work(
    work_id="https://openalex.org/W4022222222",
    title="Crystallographic packing of collagen fibrils in bovine tendon tissue",
    authors=["C. Investigator"],
    year=2009,
    doi="10.5555/dna2",
    abstract="X-ray diffraction of tendon reveals periodic fibril packing",
)

_DNA_DISTRACTOR_3 = _openalex_work(
    work_id="https://openalex.org/W4033333333",
    title="NMR spectroscopy of RNA aptamer binding pockets in solution",
    authors=["D. Chemist"],
    year=2018,
    doi="10.5555/dna3",
    abstract="Solution NMR was used to characterize aptamer conformational dynamics",
)


def test_watson_crick_real_paper_now_verified():
    work, _ = _verify_single(_WATSON_CRICK_WORK, query=_WATSON_CRICK_QUERY)
    assert work.state == VerificationState.VERIFIED, work.gate_results
    assert work.gate_results["G6_identity_match"] is True


def test_watson_crick_dna_distractors_now_rejected():
    for distractor in (_DNA_DISTRACTOR_1, _DNA_DISTRACTOR_2, _DNA_DISTRACTOR_3):
        work, _ = _verify_single(distractor, query=_WATSON_CRICK_QUERY)
        assert work.state == VerificationState.REJECTED, (distractor["title"], work.gate_results)
        assert work.gate_results["G6_identity_match"] is False


# ---------------------------------------------------------------------------
# Case 3 (bonus POSITIVE_CONTROL, S025 in the report): He et al. 2016,
# "Deep Residual Learning for Image Recognition" (ResNet) vs. unrelated
# real-paper distractors from the report (plant-disease detection, SchNet,
# Grad-CAM).
# ---------------------------------------------------------------------------

_RESNET_QUERY = "He K, Zhang X, Ren S, Sun J (2016). Deep Residual Learning for Image Recognition. CVPR."

_RESNET_WORK = _openalex_work(
    work_id="https://openalex.org/W2949650786",
    title="Deep Residual Learning for Image Recognition",
    authors=["Kaiming He", "Xiangyu Zhang", "Shaoqing Ren", "Jian Sun"],
    year=2016,
    doi="10.1109/CVPR.2016.90",
    abstract="We present a residual learning framework to ease the training of deep networks",
)

_RESNET_DISTRACTOR_SCHNET = _openalex_work(
    work_id="https://openalex.org/W4044444444",
    title="SchNet: A continuous-filter convolutional neural network for modeling quantum interactions",
    authors=["Kristof Schutt", "Pieter-Jan Kindermans"],
    year=2017,
    doi="10.5555/schnet",
    abstract="A deep learning architecture that models quantum interactions",
)

_RESNET_DISTRACTOR_PLANT = _openalex_work(
    work_id="https://openalex.org/W4055555555",
    title="Automated detection of tomato leaf disease using convolutional classifiers",
    authors=["E. Agri", "F. Botanist"],
    year=2019,
    doi="10.5555/plant",
    abstract="A classifier for common tomato leaf diseases from field images",
)


def test_resnet_real_paper_now_verified():
    work, _ = _verify_single(_RESNET_WORK, query=_RESNET_QUERY)
    assert work.state == VerificationState.VERIFIED, work.gate_results
    assert work.gate_results["G6_identity_match"] is True


def test_resnet_distractors_now_rejected():
    for distractor in (_RESNET_DISTRACTOR_SCHNET, _RESNET_DISTRACTOR_PLANT):
        work, _ = _verify_single(distractor, query=_RESNET_QUERY)
        assert work.state == VerificationState.REJECTED, (distractor["title"], work.gate_results)
        assert work.gate_results["G6_identity_match"] is False


# ---------------------------------------------------------------------------
# Case 4 (task item 1, Thai-relevance tagging): THAI_LANGUAGE and
# FOREIGN_ABOUT_THAILAND. Classification only, per
# src/thaicite/normalize/thai_relevance.py -- never a verification gate, so
# these are checked directly against `classify_thai_relevance`, not `verify`.
# ---------------------------------------------------------------------------


def test_thai_language_title_tags_thai_language():
    thai_work = _openalex_work(
        work_id="https://openalex.org/W5011111111",
        title="ปัญหาการสื่อสารระหว่างบุคลากรทางการแพทย์และผู้ป่วยในโรงพยาบาลชุมชน",
        authors=["สมชาย ใจดี"],
        year=2020,
        doi="10.5555/thai1",
        abstract="งานวิจัยนี้ศึกษาปัญหาการสื่อสารระหว่างบุคลากรทางการแพทย์และผู้ป่วย",
        country_codes=["TH"],
        host_org="Thailand Ministry of Public Health",
        landing_page="https://example.ac.th/article/1",
    )
    candidate = _candidate_from_work(thai_work)
    tags = classify_thai_relevance(candidate)
    assert THAI_LANGUAGE in tags, tags


def test_foreign_authors_studying_thailand_tags_foreign_about_thailand():
    foreign_work = _openalex_work(
        work_id="https://openalex.org/W5022222222",
        title="Tourism development impacts on coastal communities in southern Thailand",
        authors=["Jane Smith", "Robert Johnson"],
        year=2019,
        doi="10.5555/thai2",
        abstract=(
            "This study examines the socioeconomic impacts of tourism "
            "development on coastal communities near Phuket Thailand"
        ),
        country_codes=["GB"],
        host_org="University of Cambridge",
        landing_page="https://example.co.uk/article/2",
    )
    candidate = _candidate_from_work(foreign_work)
    tags = classify_thai_relevance(candidate)
    assert ABOUT_THAILAND in tags, tags
    assert FOREIGN_ABOUT_THAILAND in tags, tags
    assert THAI_LANGUAGE not in tags, tags
    assert PUBLISHED_IN_THAILAND not in tags, tags
