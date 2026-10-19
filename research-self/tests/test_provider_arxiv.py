"""Offline tests for the official arXiv Atom provider.

Everything here runs without network access: the feed fixture is a **verbatim
capture** of a real `co:"ICML 2024"` response from the arXiv Atom API (3 entries,
`<opensearch:totalResults>` 1167), so the parser is exercised against the exact
whitespace, namespace declarations and markup arXiv actually emits. HTTP is faked
with `httpx.MockTransport`, which keeps the retry/pagination logic under test
while never touching the network.
"""
from __future__ import annotations

import httpx
import pytest

from paper_ingest import db
from paper_ingest.provider_arxiv import (
    MAX_PAGE_SIZE,
    ROLLING_JOURNAL_VENUES,
    VENUE_COMMENT_PATTERNS,
    ArxivProvider,
    clean_text,
    clean_title,
    comment_queries,
    extract_arxiv_id,
    matches_venue,
    parse_total_results,
)

# --------------------------------------------------------------------------
# Real captured Atom feed: GET http://export.arxiv.org/api/query
#   ?search_query=co:"ICML 2024"&max_results=3&start=0&sortBy=relevance
# Captured live; do not reformat (the hard-wrapped titles/abstracts are the point).
# --------------------------------------------------------------------------
REAL_ATOM_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <id>https://arxiv.org/api/VxPNt4xvaRWszM7+oPPChG3/ptU</id>
  <title>arXiv Query: search_query=co:"ICML 2024"&amp;id_list=&amp;start=0&amp;max_results=3</title>
  <updated>2026-10-19T05:21:31Z</updated>
  <link href="https://arxiv.org/api/query?search_query=co:%22ICML+2024%22&amp;start=0&amp;max_results=3&amp;id_list=" type="application/atom+xml"/>
  <opensearch:itemsPerPage>3</opensearch:itemsPerPage>
  <opensearch:totalResults>1167</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
  <entry>
    <id>http://arxiv.org/abs/2311.01455v3</id>
    <title>RoboGen: Towards Unleashing Infinite Data for Automated Robot Learning via Generative Simulation</title>
    <updated>2024-06-14T21:09:49Z</updated>
    <link href="https://arxiv.org/abs/2311.01455v3" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/2311.01455v3" rel="related" type="application/pdf" title="pdf"/>
    <summary>We present RoboGen, a generative robotic agent that automatically learns diverse robotic skills at scale via generative simulation. RoboGen leverages the latest advancements in foundation and generative models. Instead of directly using or adapting these models to produce policies or low-level actions, we advocate for a generative scheme, which uses these models to automatically generate diversified tasks, scenes, and training supervisions, thereby scaling up robotic skill learning with minimal human supervision. Our approach equips a robotic agent with a self-guided propose-generate-learn cycle: the agent first proposes interesting tasks and skills to develop, and then generates corresponding simulation environments by populating pertinent objects and assets with proper spatial configurations. Afterwards, the agent decomposes the proposed high-level task into sub-tasks, selects the optimal learning approach (reinforcement learning, motion planning, or trajectory optimization), generates required training supervision, and then learns policies to acquire the proposed skill. Our work attempts to extract the extensive and versatile knowledge embedded in large-scale models and transfer them to the field of robotics. Our fully generative pipeline can be queried repeatedly, producing an endless stream of skill demonstrations associated with diverse tasks and environments.</summary>
    <category term="cs.RO" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CV" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <published>2023-11-02T17:59:21Z</published>
    <arxiv:comment>ICML 2024</arxiv:comment>
    <arxiv:primary_category term="cs.RO"/>
    <author>
      <name>Yufei Wang</name>
    </author>
    <author>
      <name>Zhou Xian</name>
    </author>
    <author>
      <name>Feng Chen</name>
    </author>
    <author>
      <name>Tsun-Hsuan Wang</name>
    </author>
    <author>
      <name>Yian Wang</name>
    </author>
    <author>
      <name>Katerina Fragkiadaki</name>
    </author>
    <author>
      <name>Zackory Erickson</name>
    </author>
    <author>
      <name>David Held</name>
    </author>
    <author>
      <name>Chuang Gan</name>
    </author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2406.19320v1</id>
    <title>Efficient World Models with Context-Aware Tokenization</title>
    <updated>2024-06-27T16:54:12Z</updated>
    <link href="https://arxiv.org/abs/2406.19320v1" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/2406.19320v1" rel="related" type="application/pdf" title="pdf"/>
    <summary>Scaling up deep Reinforcement Learning (RL) methods presents a significant challenge. Following developments in generative modelling, model-based RL positions itself as a strong contender. Recent advances in sequence modelling have led to effective transformer-based world models, albeit at the price of heavy computations due to the long sequences of tokens required to accurately simulate environments. In this work, we propose $Δ$-IRIS, a new agent with a world model architecture composed of a discrete autoencoder that encodes stochastic deltas between time steps and an autoregressive transformer that predicts future deltas by summarizing the current state of the world with continuous tokens. In the Crafter benchmark, $Δ$-IRIS sets a new state of the art at multiple frame budgets, while being an order of magnitude faster to train than previous attention-based approaches. We release our code and models at https://github.com/vmicheli/delta-iris.</summary>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CV" scheme="http://arxiv.org/schemas/atom"/>
    <published>2024-06-27T16:54:12Z</published>
    <arxiv:comment>ICML 2024</arxiv:comment>
    <arxiv:primary_category term="cs.LG"/>
    <author>
      <name>Vincent Micheli</name>
    </author>
    <author>
      <name>Eloi Alonso</name>
    </author>
    <author>
      <name>François Fleuret</name>
    </author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2407.00529v1</id>
    <title>Detecting and Identifying Selection Structure in Sequential Data</title>
    <updated>2024-06-29T20:56:34Z</updated>
    <link href="https://arxiv.org/abs/2407.00529v1" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/2407.00529v1" rel="related" type="application/pdf" title="pdf"/>
    <summary>We argue that the selective inclusion of data points based on latent objectives is common in practical situations, such as music sequences. Since this selection process often distorts statistical analysis, previous work primarily views it as a bias to be corrected and proposes various methods to mitigate its effect. However, while controlling this bias is crucial, selection also offers an opportunity to provide a deeper insight into the hidden generation process, as it is a fundamental mechanism underlying what we observe. In particular, overlooking selection in sequential data can lead to an incomplete or overcomplicated inductive bias in modeling, such as assuming a universal autoregressive structure for all dependencies. Therefore, rather than merely viewing it as a bias, we explore the causal structure of selection in sequential data to delve deeper into the complete causal process. Specifically, we show that selection structure is identifiable without any parametric assumptions or interventional experiments. Moreover, even in cases where selection variables coexist with latent confounders, we still establish the nonparametric identifiability under appropriate structural conditions. Meanwhile, we also propose a provably correct algorithm to detect and identify selection structures as well as other types of dependencies. The framework has been validated empirically on both synthetic data and real-world music.</summary>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.SD" scheme="http://arxiv.org/schemas/atom"/>
    <category term="eess.AS" scheme="http://arxiv.org/schemas/atom"/>
    <category term="math.ST" scheme="http://arxiv.org/schemas/atom"/>
    <category term="stat.ML" scheme="http://arxiv.org/schemas/atom"/>
    <published>2024-06-29T20:56:34Z</published>
    <arxiv:comment>ICML 2024</arxiv:comment>
    <arxiv:primary_category term="cs.LG"/>
    <author>
      <name>Yujia Zheng</name>
    </author>
    <author>
      <name>Zeyu Tang</name>
    </author>
    <author>
      <name>Yiwen Qiu</name>
    </author>
    <author>
      <name>Bernhard Schölkopf</name>
    </author>
    <author>
      <name>Kun Zhang</name>
    </author>
  </entry>
</feed>
"""

_HEADER = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <id>https://arxiv.org/api/synthetic</id>
  <title>arXiv Query</title>
  <updated>2026-10-19T05:21:31Z</updated>
  <opensearch:totalResults>{total}</opensearch:totalResults>
  <opensearch:startIndex>{start}</opensearch:startIndex>
"""

_HEADER_NO_TOTAL = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <id>https://arxiv.org/api/synthetic</id>
  <title>arXiv Query</title>
  <updated>2026-10-19T05:21:31Z</updated>
  <opensearch:startIndex>{start}</opensearch:startIndex>
"""

# --------------------------------------------------------------------------
# Test helpers
# --------------------------------------------------------------------------

REQUIRED_DB_FIELDS = (
    "id", "source", "source_paper_id", "forum_id", "title", "abstract", "authors",
    "keywords", "primary_area", "pdf_url", "paper_url", "doi", "arxiv_id",
    "submission_year", "publication_year", "status", "decision",
    "created_at_source", "updated_at_source", "raw_data_path",
)


def make_entry(arxiv_id: str, title: str = "A Title", comment: str = "ICML 2024",
               published: str = "2024-01-15T00:00:00Z",
               journal_ref: str | None = None) -> str:
    """Build one synthetic `<entry>` in the same shape arXiv emits."""
    comment_xml = f"<arxiv:comment>{comment}</arxiv:comment>" if comment else ""
    journal_xml = (
        f"<arxiv:journal_ref>{journal_ref}</arxiv:journal_ref>" if journal_ref else ""
    )
    return f"""  <entry>
    <id>http://arxiv.org/abs/{arxiv_id}v1</id>
    <title>{title}</title>
    <updated>2024-02-01T00:00:00Z</updated>
    <link href="https://arxiv.org/abs/{arxiv_id}v1" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/{arxiv_id}v1" rel="related" type="application/pdf" title="pdf"/>
    <summary>Abstract for {arxiv_id}.</summary>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <published>{published}</published>
    {comment_xml}
    {journal_xml}
    <arxiv:primary_category term="cs.LG"/>
    <author><name>Author {arxiv_id}</name></author>
  </entry>
"""


def make_feed(entries: list[str], total: int | None = None, start: int = 0,
              *, include_total: bool = True) -> str:
    """Wrap synthetic entries in an Atom feed envelope with opensearch metadata."""
    total = len(entries) if total is None else total
    body = "\n".join(entries)
    header = _HEADER if include_total else _HEADER_NO_TOTAL
    return header.format(total=total, start=start) + body + "\n</feed>\n"


def mock_provider(handler, **kwargs) -> ArxivProvider:
    """Build a provider backed by an in-memory httpx transport (no sockets)."""
    kwargs.setdefault("delay", 0.0)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ArxivProvider(client=client, **kwargs)
    # Backoff sleeps would make retry tests wall-clock slow; the retry *behaviour*
    # is what matters, not the literal pause.
    provider._sleep_backoff = lambda *args, **kwargs: None  # type: ignore[method-assign]
    return provider


# --------------------------------------------------------------------------
# (a) parse_feed on the real captured Atom entry
# --------------------------------------------------------------------------


def test_parse_feed_extracts_fields_from_real_atom_entry():
    provider = ArxivProvider(delay=0)
    entries = provider.parse_feed(REAL_ATOM_FEED)

    assert len(entries) == 3
    entry = entries[0]

    # Version suffix stripped: identity must be stable across re-harvests.
    assert entry["arxiv_id"] == "2311.01455"
    assert entry["entry_id"] == "http://arxiv.org/abs/2311.01455v3"
    assert entry["title"] == (
        "RoboGen: Towards Unleashing Infinite Data for Automated Robot Learning "
        "via Generative Simulation"
    )
    assert entry["abstract"].startswith("We present RoboGen, a generative robotic agent")
    assert entry["abstract"].endswith("diverse tasks and environments.")
    # The real feed hard-wraps the summary; everything must be single-spaced.
    assert "\n" not in entry["abstract"] and "  " not in entry["abstract"]
    assert len(entry["authors"]) == 9
    assert entry["authors"][0] == "Yufei Wang"
    assert entry["authors"][-1] == "Chuang Gan"
    # arxiv: namespace elements resolved through the namespace map, not regex.
    assert entry["comment"] == "ICML 2024"
    assert entry["primary_category"] == "cs.RO"
    assert entry["categories"] == ["cs.RO", "cs.AI", "cs.CV", "cs.LG"]
    # pdf link is the one tagged title="pdf"; paper_url is rel="alternate".
    assert entry["pdf_url"] == "https://arxiv.org/pdf/2311.01455v3"
    assert entry["paper_url"] == "https://arxiv.org/abs/2311.01455v3"
    assert entry["published"] == "2023-11-02T17:59:21Z"
    assert entry["updated"] == "2024-06-14T21:09:49Z"
    assert entry["journal_ref"] == ""
    assert entry["doi"] == ""

    # Non-ASCII author names survive decoding (Bernhard Schölkopf).
    assert "Bernhard Schölkopf" in entries[2]["authors"]
    assert parse_total_results(REAL_ATOM_FEED) == 1167


def test_parse_feed_returns_empty_for_empty_feed_and_raises_on_garbage():
    provider = ArxivProvider(delay=0)
    assert provider.parse_feed(make_feed([], total=0)) == []
    with pytest.raises(ValueError):
        provider.parse_feed("<feed><entry>not closed")


# --------------------------------------------------------------------------
# (b) clean_text / clean_title
# --------------------------------------------------------------------------


def test_clean_text_collapses_newlines_and_whitespace():
    assert clean_text("Line one\n    line two\t\tline three") == "Line one line two line three"
    assert clean_text("  padded  ") == "padded"
    assert clean_text("") == ""
    assert clean_text(None) == ""
    # Trailing period on titles is not part of the published title.
    assert clean_title("A Title Ends.") == "A Title Ends"
    assert clean_title("A Title, No Period") == "A Title, No Period"
    assert clean_title("  Multi\n line .  ") == "Multi line"


# --------------------------------------------------------------------------
# (c) matches_venue: real acceptance wording accepted, other years/venues rejected
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "comment,journal_ref,venue,year",
    [
        ("ICML 2024", "", "icml", 2024),
        ("In ICLR 2024", "", "iclr", 2024),
        ("Accepted at ICML 2024", "", "icml", 2024),
        ("Accepted to NeurIPS 2024", "", "neurips", 2024),
        ("accepted at icml 2024", "", "icml", 2024),          # case differences
        ("Published at NeurIPS 2024", "", "neurips", 2024),
        ("to appear at ICML 2024", "", "icml", 2024),
        ("ICML 2024 camera-ready", "", "icml", 2024),
        ("ICML-2024", "", "icml", 2024),
        ("ICML'24", "", "icml", 2024),
        ("ICML2024", "", "icml", 2024),
        ("", "ICML 2024", "icml", 2024),                       # journal_ref only
        ("NIPS 2023", "", "neurips", 2023),                    # alias + own year
        # Live-sampled wording: a "submitted" word does not by itself mean rejected.
        ("submitted and accepted at ICML 2024", "", "icml", 2024),
        ("ICML 2024 (Spotlight)", "", "icml", 2024),
        ("ICML'24 Oral", "", "icml", 2024),
    ],
)
def test_matches_venue_accepts_real_acceptance_wording(comment, journal_ref, venue, year):
    assert matches_venue(comment, journal_ref, venue, year) is True


@pytest.mark.parametrize(
    "comment,journal_ref,venue,year",
    [
        ("ICML 2023", "", "icml", 2024),                  # wrong year, right venue
        ("ICML 2024", "", "icml", 2025),                  # year too early in comment
        ("ICML 2024", "", "iclr", 2024),                  # wrong venue
        ("ICLR 2024", "", "icml", 2024),
        ("", "", "icml", 2024),                           # nothing to verify
        ("SNIPS 2024", "", "neurips", 2024),              # alias must be a whole token
        ("ICML 20245", "", "icml", 2024),                 # longer number is not the year
        # Live-sampled false positives: venue-adjacent, but not main-track papers.
        ("AI4Math workshop, ICML 2024", "", "icml", 2024),
        ("accepted in ML4LMS workshop at ICML 2024", "", "icml", 2024),
        ("ICML 2024 workshop on LLMs and Cognition", "", "icml", 2024),
        ("video appeared in ICML 2024 tutorial", "", "icml", 2024),
        ("under review at ICML 2024", "", "icml", 2024),
        ("submitted to ICML 2024", "", "icml", 2024),
    ],
)
def test_matches_venue_rejects_wrong_year_venue_and_workshops(comment, journal_ref, venue, year):
    assert matches_venue(comment, journal_ref, venue, year) is False


def test_matches_venue_spotlight_and_oral_are_main_track():
    """Spotlight/oral notes are main-conference acceptances and must not be dropped."""
    assert matches_venue("ICML 2024 (Spotlight)", "", "icml", 2024) is True
    assert matches_venue("ICML'24 Oral", "", "icml", 2024) is True


# --------------------------------------------------------------------------
# Query generation: year-generic, covers 2022-2026, no giant hardcoded literal
# --------------------------------------------------------------------------


def test_comment_queries_are_generated_per_venue_and_year():
    queries = comment_queries("icml", 2024)
    assert queries[0] == 'co:"ICML 2024"'
    assert 'co:"In ICML 2024"' in queries
    assert 'co:"accepted at ICML 2024"' in queries
    assert 'co:"ICML 2024 camera ready"' in queries
    assert 'co:"ICML\'24"' in queries          # distinct-token form, adds recall
    assert len(queries) == len(set(queries))    # no duplicate queries
    # Every generated query is a quote-wrapped comment-field search.
    assert all(q.startswith('co:"') and q.endswith('"') for q in queries)

    # Year-generic: an arbitrary future/past year needs no code change.
    assert 'co:"ICML 2031"' in comment_queries("icml", 2031)
    assert 'co:"ICML\'31"' in comment_queries("icml", 2031)
    assert 'co:"ICML 2022"' in comment_queries("icml", 2022)

    # Alias venues query both spellings authors actually use.
    neurips = comment_queries("neurips", 2024)
    assert 'co:"NeurIPS 2024"' in neurips and 'co:"NIPS 2024"' in neurips


def test_venue_comment_patterns_cover_all_venues_and_years():
    assert set(VENUE_COMMENT_PATTERNS) >= {"icml", "iclr", "neurips", "cvpr", "acl", "tmlr"}
    for venue, queries in VENUE_COMMENT_PATTERNS.items():
        assert queries, f"{venue} has no comment queries"
        assert all(q.startswith('co:"') and q.endswith('"') for q in queries)
        rendered = " ".join(queries)
        if venue in ROLLING_JOURNAL_VENUES:
            # Rolling journals have no "Venue <year>" cohort. Measured live:
            # `co:"TMLR 2024"` returns nothing usable while `co:"TMLR"` has
            # ~1315 hits, so year-less queries are correct here (and the year is
            # enforced later from each paper's own publication date).
            assert not any(str(y) in rendered for y in (2022, 2023, 2024, 2025, 2026))
        else:
            for year in (2022, 2023, 2024, 2025, 2026):
                assert str(year) in rendered, f"{venue} missing year {year}"
        assert len(queries) == len(set(queries))


# --------------------------------------------------------------------------
# (d) de-duplication across duplicate queries
# --------------------------------------------------------------------------


def test_search_venue_deduplicates_papers_across_queries():
    """Every generated query returns the same feed, yet each paper is kept once."""
    seen_queries: list[str] = []
    feed = make_feed([make_entry("2401.00001"), make_entry("2401.00002")], total=2)

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.params["search_query"])
        return httpx.Response(200, text=feed)

    provider = mock_provider(handler)
    papers = provider.search_venue("icml", 2024)

    assert len(seen_queries) == len(comment_queries("icml", 2024)) > 1
    assert [p["arxiv_id"] for p in papers] == ["2401.00001", "2401.00002"]
    assert all(p["id"].startswith("arxiv:") for p in papers)
    assert len({p["id"] for p in papers}) == len(papers)
    assert provider.last_stats["accepted"] == 2


def test_search_deduplicates_repeated_ids_across_pages():
    pages = {
        0: make_feed([make_entry("2401.00001"), make_entry("2401.00002")], total=3),
        2: make_feed([make_entry("2401.00002"), make_entry("2401.00003")], total=3),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=pages[int(request.url.params["start"])])

    provider = mock_provider(handler, per_page=2)
    entries = provider.search('co:"ICML 2024"')
    assert [e["arxiv_id"] for e in entries] == ["2401.00001", "2401.00002", "2401.00003"]


# --------------------------------------------------------------------------
# (e) pagination honours max_results and stops at totalResults
# --------------------------------------------------------------------------


def test_search_paginates_and_stops_at_total_results():
    ids = [f"2401.0000{i}" for i in range(1, 6)]  # 5 papers, page size 2
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        size = int(request.url.params["max_results"])
        starts.append(start)
        return httpx.Response(
            200, text=make_feed(
                [make_entry(i) for i in ids[start:start + size]], total=5, start=start
            )
        )

    provider = mock_provider(handler, per_page=2)
    entries = provider.search('co:"ICML 2024"')

    assert [e["arxiv_id"] for e in entries] == ids        # all 5, in feed order
    assert starts == [0, 2, 4]                            # stops at totalResults, no 4th page


def test_search_respects_max_results_cap():
    ids = [f"2401.0000{i}" for i in range(1, 11)]
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        size = int(request.url.params["max_results"])
        starts.append(start)
        return httpx.Response(
            200, text=make_feed(
                [make_entry(i) for i in ids[start:start + size]], total=10, start=start
            )
        )

    provider = mock_provider(handler, per_page=2)
    entries = provider.search('co:"ICML 2024"', max_results=3)

    assert len(entries) == 3
    assert [e["arxiv_id"] for e in entries] == ids[:3]
    assert starts == [0, 2]        # 2 + 1, then stop: no over-fetching
    assert provider.search('co:"ICML 2024"', max_results=0) == []


def test_search_stops_on_short_page_without_total_results():
    """Some responses omit totalResults; a short page must still terminate the walk."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        calls.append(start)
        if start == 0:
            return httpx.Response(200, text=make_feed(
                [make_entry("2401.00001"), make_entry("2401.00002")], include_total=False))
        return httpx.Response(200, text=make_feed([make_entry("2401.00003")], include_total=False))

    provider = mock_provider(handler, per_page=2)
    entries = provider.search('co:"ICML 2024"', max_results=4)
    assert [e["arxiv_id"] for e in entries] == ["2401.00001", "2401.00002", "2401.00003"]
    assert calls == [0, 2]


def test_per_page_is_clamped_to_arxiv_limit():
    assert ArxivProvider(delay=0, per_page=5000).per_page == MAX_PAGE_SIZE
    assert ArxivProvider(delay=0, per_page=0).per_page == 1


# --------------------------------------------------------------------------
# (f) malformed entries are skipped, not fatal
# --------------------------------------------------------------------------


def test_malformed_entries_are_skipped_not_fatal():
    no_title = """  <entry>
    <id>http://arxiv.org/abs/2401.99999v1</id>
    <summary>No title here.</summary>
    <arxiv:primary_category term="cs.LG"/>
  </entry>
"""
    no_id = """  <entry>
    <title>No id here</title>
    <summary>Nothing to identify.</summary>
  </entry>
"""
    feed = make_feed([make_entry("2401.00001"), no_title, no_id], total=3)

    provider = ArxivProvider(delay=0)
    entries = provider.parse_feed(feed)
    assert [e["arxiv_id"] for e in entries] == ["2401.00001"]


def test_malformed_entry_does_not_abort_search_venue():
    feed = make_feed([make_entry("2401.00001"), "<entry><title>broken", ], total=2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=feed)

    provider = mock_provider(handler)
    # `broken` makes the whole document unparseable; fail-soft means no exception
    # escapes and the caller still gets a (here empty) list rather than a crash.
    papers = provider.search_venue("icml", 2024)
    assert papers == []


# --------------------------------------------------------------------------
# Robustness: failed pages / retries never abort the harvest
# --------------------------------------------------------------------------


def test_search_retries_on_server_error_then_succeeds():
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(200, text=make_feed([make_entry("2401.00001")], total=1))

    provider = mock_provider(handler, max_retries=3)
    entries = provider.search('co:"ICML 2024"')
    assert [e["arxiv_id"] for e in entries] == ["2401.00001"]
    assert len(attempts) == 2


def test_search_returns_partial_results_when_later_page_fails():
    """A failed page must not discard what was already collected."""
    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        if start == 0:
            return httpx.Response(200, text=make_feed(
                [make_entry("2401.00001"), make_entry("2401.00002")], total=6))
        return httpx.Response(500, text="boom")

    provider = mock_provider(handler, per_page=2, max_retries=2)
    entries = provider.search('co:"ICML 2024"', max_results=6)
    assert [e["arxiv_id"] for e in entries] == ["2401.00001", "2401.00002"]


def test_search_venue_rejects_non_matching_comments_from_the_api():
    """The precision gate runs locally: workshop papers must never reach the DB shape."""
    feed = make_feed([
        make_entry("2401.00001", comment="ICML 2024"),
        make_entry("2401.00002", comment="AI4Math workshop, ICML 2024"),
        make_entry("2401.00003", comment="ICML 2023"),
    ], total=3)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=feed)

    provider = mock_provider(handler)
    papers = provider.search_venue("icml", 2024, max_results=10)
    assert [p["arxiv_id"] for p in papers] == ["2401.00001"]
    assert provider.last_stats["rejected"] >= 2


def test_search_venue_output_is_deterministically_ordered():
    feed = make_feed([
        make_entry("2401.00009"), make_entry("2401.00003"), make_entry("2401.00007"),
    ], total=3)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=feed)

    provider = mock_provider(handler)
    first = provider.search_venue("icml", 2024)
    second = mock_provider(handler).search_venue("icml", 2024)
    assert [p["arxiv_id"] for p in first] == ["2401.00003", "2401.00007", "2401.00009"]
    assert first == second


# --------------------------------------------------------------------------
# (g) normalize fills every required DB field
# --------------------------------------------------------------------------


def test_normalize_fills_every_required_db_field():
    provider = ArxivProvider(delay=0)
    entry = provider.parse_feed(REAL_ATOM_FEED)[0]
    paper = provider.normalize(entry, "icml", 2024)

    assert set(REQUIRED_DB_FIELDS) <= set(paper)
    assert paper["id"] == "arxiv:2311.01455"
    assert paper["source"] == "arxiv"
    assert paper["source_paper_id"] == "2311.01455"
    assert paper["forum_id"] is None                    # arXiv has no forum thread
    assert paper["arxiv_id"] == "2311.01455"
    assert paper["title"].endswith("Generative Simulation")
    assert len(paper["abstract"]) > 1000
    assert paper["authors"][0] == "Yufei Wang"
    assert paper["keywords"] == ["cs.RO", "cs.AI", "cs.CV", "cs.LG"]
    assert paper["primary_area"] == "cs.RO"
    assert paper["pdf_url"] == "https://arxiv.org/pdf/2311.01455v3"
    assert paper["paper_url"] == "https://arxiv.org/abs/2311.01455v3"
    assert paper["doi"] is None
    assert paper["submission_year"] == 2023              # arXiv v1 year
    assert paper["publication_year"] == 2024             # venue year under harvest
    assert paper["status"] == "published"
    assert paper["decision"] is None
    assert paper["created_at_source"] == "2023-11-02T17:59:21Z"
    assert paper["updated_at_source"] == "2024-06-14T21:09:49Z"
    assert paper["raw_data_path"] is None


def test_normalized_paper_round_trips_through_db_upsert(tmp_path, monkeypatch):
    """End-to-end proof the dict shape is what `db.upsert_paper` actually needs."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "papers.sqlite")
    monkeypatch.setattr(db, "DATA_ROOT", tmp_path)

    provider = ArxivProvider(delay=0)
    entry = provider.parse_feed(REAL_ATOM_FEED)[0]
    paper = provider.normalize(entry, "icml", 2024)

    db.init_db()
    with db.connect() as con:
        db.put_venue(con, venue_id="icml", name="ICML", year=2024, provider="arxiv",
                     source_venue_id=None, kind="conference", source_url=None,
                     availability="available")
        assert db.upsert_paper(con, paper, "icml", 2024, relation_type="publication") == "inserted"
        row = con.execute("SELECT * FROM papers WHERE id=?", (paper["id"],)).fetchone()
        assert row["source"] == "arxiv"
        assert row["arxiv_id"] == "2311.01455"
        assert row["primary_area"] == "cs.RO"
        assert row["publication_year"] == 2024
        assert row["status"] == "published"
        assert con.execute(
            "SELECT count(*) FROM paper_venues WHERE paper_id=? AND venue_id=?",
            (paper["id"], "icml"),
        ).fetchone()[0] == 1
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        # Re-harvesting the same paper must update, not duplicate.
        assert db.upsert_paper(con, paper, "icml", 2024, relation_type="publication") == "updated"


def test_extract_arxiv_id_handles_versions_and_legacy_ids():
    assert extract_arxiv_id("http://arxiv.org/abs/2311.01455v3") == "2311.01455"
    assert extract_arxiv_id("https://arxiv.org/abs/2406.19320") == "2406.19320"
    assert extract_arxiv_id("http://arxiv.org/abs/cs/0112017v1") == "cs/0112017"
    assert extract_arxiv_id("") == ""
    assert extract_arxiv_id(None) == ""


def test_provider_context_manager_and_injected_client_ownership():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<feed/>")))
    with ArxivProvider(delay=0, client=client) as provider:
        assert provider._owns_client is False
    assert client.is_closed is False        # caller keeps ownership of injected client
    client.close()
