"""Tests for the JMLR harvester (paper_ingest.jmlr).

The parser is exercised against the publisher's real markup shape (captured
from jmlr.org volume pages), so a markup change shows up as a failing test
rather than as a silently empty import.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_ingest import jmlr  # noqa: E402

# A trimmed but structurally faithful copy of a jmlr.org volume page entry.
VOLUME_HTML = """
<h1>JMLR Volume 24</h1>
<dl>
<dt>Approximation Bounds for Hierarchical Clustering: Average Linkage, Bisecting K-means, and Local Search</dt>
<dd><b><i>Benjamin Moseley, Joshua R. Wang</i></b>; (1):1&minus;36, 2023.
<br>[<a href='/papers/v24/18-080.html'>abs</a>][<a target=_blank href='/papers/volume24/18-080/18-080.pdf'>pdf</a>][<a href="/papers/v24/18-080.bib">bib</a>]
</dl>
<dl>
<dt>Another Paper With A Title</dt>
<dd><b><i>Solo Author</i></b>; (2):100&minus;120, 2024.
<br>[<a href='/papers/v24/19-999.html'>abs</a>][<a target=_blank href='/papers/volume24/19-999/19-999.pdf'>pdf</a>][<a href="/papers/v24/19-999.bib">bib</a>]
</dl>
"""

PAPER_HTML = """
<html><head>
<meta name="citation_title" content="Approximation Bounds for Hierarchical Clustering">
<meta name="citation_author" content="Benjamin Moseley">
<meta name="citation_author" content="Joshua R. Wang">
<meta name="citation_journal_title" content="Journal of Machine Learning Research">
<meta name="citation_volume" content="24">
<meta name="citation_issue" content="1">
<meta name="citation_firstpage" content="1">
<meta name="citation_lastpage" content="36">
<meta name="citation_publication_date" content="2023">
<meta name="citation_issn" content="1533-7928">
</head><body>
<h2> Approximation Bounds for Hierarchical Clustering </h2>
<p><b><i>Benjamin Moseley, Joshua R. Wang</i></b>; 24(1):1&minus;36, 2023.</p>
<h3>Abstract</h3>
<p class="abstract">
Hierarchical clustering is a data analysis method that has been used for decades. Despite its
widespread use, the method has an underdeveloped analytical foundation, which this paper
addresses with an analytic framework for understanding observations seen in practice.
</p>
</body></html>
"""


def _entries(html: str) -> list[dict]:
    """Parse a volume page body without touching the network."""
    import re
    entries = []
    for block in jmlr._ENTRY_RE.findall(html):
        title_match = jmlr._TITLE_RE.search(block)
        if not title_match:
            continue
        abs_match = jmlr._ABS_RE.search(block)
        if not abs_match:
            continue
        year_match = jmlr._CITATION_RE.search(block)
        if not year_match:
            continue
        authors_match = jmlr._AUTHORS_RE.search(block)
        entries.append({
            "title": jmlr._clean(title_match.group(1)),
            "authors": [a.strip() for a in jmlr._clean(authors_match.group(1)).split(",")] if authors_match else [],
            "year": int(year_match.group(2)),
            "pages": year_match.group(1).replace("&minus;", "-"),
            "abs_url": "https://jmlr.org" + abs_match.group(1),
            "paper_id": abs_match.group(1).rsplit("/", 1)[-1].rsplit(".", 1)[0],
            "pdf_url": None, "bib_url": None, "volume": 24,
        })
    return entries


def test_volume_entry_parsing():
    entries = _entries(VOLUME_HTML)
    assert len(entries) == 2
    first = entries[0]
    assert first["title"].startswith("Approximation Bounds")
    assert first["authors"] == ["Benjamin Moseley", "Joshua R. Wang"]
    assert first["year"] == 2023
    assert first["pages"] == "1-36"
    assert first["paper_id"] == "18-080"
    assert first["abs_url"] == "https://jmlr.org/papers/v24/18-080.html"
    # The year comes from the entry itself, never from a volume->year guess.
    assert entries[1]["year"] == 2024


def test_paper_page_abstract_extraction():
    import re
    match = jmlr._ABSTRACT_RE.search(PAPER_HTML)
    assert match is not None
    abstract = jmlr._clean(match.group(1))
    assert abstract.startswith("Hierarchical clustering is a data analysis method")
    assert len(abstract) > 150
    meta = {name: value for name, value in jmlr._META_RE.findall(PAPER_HTML)}
    assert meta["citation_title"].startswith("Approximation Bounds")
    assert meta["citation_issn"] == "1533-7928"
    assert meta["citation_publication_date"] == "2023"


def test_to_paper_maps_the_db_contract():
    entry = {"title": "T", "authors": ["A B"], "year": 2023, "pages": "1-36",
             "abs_url": "https://jmlr.org/papers/v24/18-080.html", "paper_id": "18-080",
             "pdf_url": None, "volume": 24}
    paper = jmlr._to_paper(entry, {"abstract": "A" * 200, "meta": {"citation_issn": "1533-7928"}})
    assert paper["id"] == "jmlr:18-080"
    assert paper["source"] == "JMLR"
    assert paper["title"] == "T"
    assert paper["authors"] == ["A B"]
    assert paper["publication_year"] == 2023
    assert paper["submission_year"] == 2023
    assert paper["status"] == "published"
    assert paper["abstract"].startswith("A")
    # The DB contract requires these keys to exist (db.upsert_paper indexes them).
    for key in ("source_paper_id", "forum_id", "keywords", "primary_area", "pdf_url",
                "paper_url", "doi", "arxiv_id", "decision", "created_at_source",
                "updated_at_source", "raw_data_path"):
        assert key in paper


def test_paper_id_is_stable_and_unique():
    """Re-running the harvest must update rows, not duplicate them."""
    entry = {"paper_id": "18-080", "year": 2023, "title": "T", "authors": [],
             "abs_url": "https://jmlr.org/papers/v24/18-080.html", "pages": "1-2",
             "pdf_url": None, "volume": 24}
    a = jmlr._to_paper(entry, {"abstract": "", "meta": {}})
    b = jmlr._to_paper(entry, {"abstract": "new", "meta": {}})
    assert a["id"] == b["id"] == "jmlr:18-080"


def test_year_outside_the_supported_range_is_rejected():
    with pytest.raises(ValueError):
        jmlr.sync(2019)
    with pytest.raises(ValueError):
        jmlr.sync(2027)


def test_supported_years_match_the_project_span():
    assert jmlr.YEARS == (2022, 2023, 2024, 2025, 2026)


def test_clean_strips_markup_and_entities():
    assert jmlr._clean("<b><i>A &amp; B</i></b>") == "A & B"
    assert jmlr._clean("  spaced\n\tout  ") == "spaced out"
    # &minus; unescapes to the real minus sign U+2212; the page-range parser
    # normalises it to a plain hyphen separately (see test_volume_entry_parsing).
    assert jmlr._clean("1&minus;36") == "1−36"
    assert jmlr._clean("1&minus;36").replace("−", "-") == "1-36"