"""Offline tests for the OpenAlex/Crossref official-API providers.

Every test here runs with **no network access**. Real HTTP is replaced by
:class:`FakeHttp`, a small scripted transport that plays back recorded OpenAlex
and Crossref payload shapes and records the requests it received, so assertions
can verify *what* was asked for (filter syntax, pagination, retries) without
touching the wire.

The payloads below are trimmed copies of real responses captured from the live
APIs (see ``_oa_smoke.py``), which is why they carry the awkward parts --
multi-position inverted indexes, ``https://doi.org/`` prefixes, JATS markup --
rather than tidy synthetic values.
"""
from __future__ import annotations

import logging
import threading
import time

import httpx
import pytest

from paper_ingest import db
from paper_ingest.providers_openalex import (
    CONTAINER_RELEVANCE_THRESHOLD,
    CROSSREF_SELECT,
    DEFAULT_MAILTO,
    OPENALEX_LOW_COVERAGE_VENUES,
    OPENALEX_SELECT,
    OPENALEX_SOURCES,
    CrossrefProvider,
    MalformedWorkError,
    OpenAlexProvider,
    PoliteHttp,
    PoliteHttpError,
    clean_markup_text,
    container_relevance,
    normalize_doi,
    rebuild_abstract,
    user_agent,
)


# --------------------------------------------------------------------------- #
# Fake transport                                                              #
# --------------------------------------------------------------------------- #
class FakeHttp:
    """Scripted stand-in for :class:`PoliteHttp`.

    ``pages`` is a list of payloads returned in order; :meth:`get_by_doi` and
    ``search_source`` consume them. It records ``(url, params)`` for every call so
    tests can assert on the filter and cursor actually sent.
    """

    def __init__(self, pages=None, *, default=None):
        self.pages = list(pages or [])
        self.default = default
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def get_json(self, url, params=None, *, select=None):
        query = dict(params or {})
        if select:
            query.setdefault("select", select)
        self.calls.append((url, query))
        if self.pages:
            return self.pages.pop(0)
        return self.default

    def close(self):
        self.closed = True

    @property
    def last_params(self) -> dict:
        assert self.calls, "no request was made"
        return self.calls[-1][1]


def oa_page(titles, *, next_cursor=None, count=None, id_offset=0):
    """Build an OpenAlex ``/works`` response envelope.

    ``id_offset`` keeps work ids unique *across* pages: without it every page
    would reuse ``W0``/``W1`` and the provider's duplicate filter (correctly)
    would drop the later pages.
    """
    return {
        "meta": {
            "count": count if count is not None else len(titles),
            "per_page": 200,
            "next_cursor": next_cursor,
            "db_response_time_ms": 12,
        },
        "results": [
            {"id": f"https://openalex.org/W{i + id_offset}", "title": t}
            for i, t in enumerate(titles)
        ],
    }


#: Sentinel distinguishing "caller passed nothing" from "caller passed None".
_UNSET = object()


def oa_work(
    wid="W4415795831",
    *,
    title="A Study of Graph Things",
    doi=_UNSET,
    year=2024,
    inverted=_UNSET,
    authorships=_UNSET,
    keywords=_UNSET,
    primary_topic=_UNSET,
    primary_location=_UNSET,
    open_access=_UNSET,
):
    """One realistic OpenAlex work record (trimmed from a live response).

    Fields default to realistic values but accept an explicit ``None`` so tests
    can model genuinely sparse records -- hence the ``_UNSET`` sentinel.
    """
    if doi is _UNSET:
        doi = "https://doi.org/10.52202/079017-3429"
    if inverted is _UNSET:
        inverted = None
    if authorships is _UNSET:
        authorships = [
            {
                "author_position": "first",
                "author": {"id": None, "display_name": "Ao Wang", "orcid": None},
                "raw_author_name": "Ao Wang",
            },
            {"author": {"id": None, "display_name": "Hui Li"}, "raw_author_name": "Hui Li"},
        ]
    if primary_location is _UNSET:
        primary_location = {
            "landing_page_url": "https://doi.org/10.52202/079017-3429",
            "pdf_url": None,
            "source": {"id": "https://openalex.org/S4363606243"},
        }
    if keywords is _UNSET:
        keywords = [
            {"id": "https://openalex.org/keywords/yolov10", "display_name": "YOLOv10", "score": 0.98},
            {"id": "https://openalex.org/keywords/object-detection", "display_name": "object detection", "score": 0.9},
        ]
    if primary_topic is _UNSET:
        primary_topic = {
            "id": "https://openalex.org/T10036",
            "display_name": "Advanced Neural Network Applications",
            "subfield": {"display_name": "Computer Vision and Pattern Recognition"},
            "field": {"display_name": "Computer Science"},
        }
    if open_access is _UNSET:
        open_access = {"is_oa": False, "oa_url": None}
    return {
        "id": f"https://openalex.org/{wid}",
        "doi": doi,
        "title": title,
        "publication_year": year,
        "type": "proceedings-article",
        "abstract_inverted_index": inverted,
        "authorships": authorships,
        "primary_location": primary_location,
        "referenced_works": ["https://openalex.org/W1", "https://openalex.org/W2"],
        "cited_by_count": 7,
        "keywords": keywords,
        "primary_topic": primary_topic,
        "created_date": "2025-11-03T00:00:00",
        "updated_date": "2026-10-18T09:31:52.927876",
        "open_access": open_access,
    }


# --------------------------------------------------------------------------- #
# (a) rebuild_abstract                                                        #
# --------------------------------------------------------------------------- #
def test_rebuild_abstract_round_trips_realistic_inverted_index():
    """Sorted positions must reproduce the original sentence exactly.

    The index deliberately repeats "the" at many positions and puts a word at a
    position used by another word -- the two cases naive inversion mishandles.
    """
    text = "the model the graph the loss and the graph encoder"
    inverted: dict[str, list[int]] = {}
    for position, token in enumerate(text.split()):
        inverted.setdefault(token, []).append(position)

    assert rebuild_abstract(inverted) == text
    # "the" really is multi-position (the case a {position: word} dict must not lose).
    assert len(inverted["the"]) == 4
    assert len(inverted["graph"]) == 2


def test_rebuild_abstract_handles_scalar_and_string_positions():
    """Position lists may arrive as scalars or numeric strings; both must work."""
    assert rebuild_abstract({"alpha": 0, "beta": "1", "gamma": [2]}) == "alpha beta gamma"
    assert rebuild_abstract({"alpha": [1.0], "beta": [0.0]}) == "beta alpha"


def test_rebuild_abstract_keeps_colliding_positions_deterministically():
    """Two words at the same index: both survive, order is stable, nothing raises.

    A dict inversion would keep only one of them; keeping both is lossless and
    makes the output reproducible (first appearance breaks the tie).
    """
    inverted = {"first": [0], "second": [0], "third": [1]}
    assert rebuild_abstract(inverted) == "first second third"
    assert rebuild_abstract(inverted) == rebuild_abstract(inverted)


@pytest.mark.parametrize("value", [None, {}, [], "not-a-dict", 5])
def test_rebuild_abstract_is_defensive_about_junk(value):
    assert rebuild_abstract(value) is None


def test_rebuild_abstract_ignores_unusable_positions():
    inverted = {"keep": [0], "drop": [None, {"x": 1}, "abc"], "also": [1]}
    assert rebuild_abstract(inverted) == "keep also"


# --------------------------------------------------------------------------- #
# (b) normalize maps every DB field                                           #
# --------------------------------------------------------------------------- #
REQUIRED_PAPER_KEYS = {
    "id",
    "source",
    "source_paper_id",
    "forum_id",
    "title",
    "abstract",
    "authors",
    "keywords",
    "primary_area",
    "pdf_url",
    "paper_url",
    "doi",
    "arxiv_id",
    "submission_year",
    "publication_year",
    "status",
    "decision",
    "created_at_source",
    "updated_at_source",
    "raw_data_path",
}


def test_normalize_covers_every_db_field():
    provider = OpenAlexProvider(http=FakeHttp())
    work = oa_work(
        inverted={"We": [0], "present": [1], "a": [2], "result": [3]},
        primary_location={
            "landing_page_url": "https://proceedings.example/paper",
            "pdf_url": "https://proceedings.example/paper.pdf",
        },
    )
    paper = provider.normalize(work, "neurips", 2024)

    # The DB contract is the whole point of this method: every column must exist.
    assert REQUIRED_PAPER_KEYS <= set(paper)
    assert paper["id"] == "openalex:W4415795831"
    assert paper["source"] == "openalex"
    assert paper["source_paper_id"] == "W4415795831"
    assert paper["title"] == "A Study of Graph Things"
    assert paper["abstract"] == "We present a result"
    assert paper["authors"] == ["Ao Wang", "Hui Li"]
    assert paper["keywords"] == ["YOLOv10", "object detection"]
    assert paper["primary_area"] == "Computer Vision and Pattern Recognition"
    assert paper["pdf_url"] == "https://proceedings.example/paper.pdf"
    assert paper["paper_url"] == "https://proceedings.example/paper"
    assert paper["doi"] == "10.52202/079017-3429"  # bare, no https://doi.org/
    assert paper["arxiv_id"] is None
    assert paper["submission_year"] == 2024
    assert paper["publication_year"] == 2024
    assert paper["status"] == "published"
    assert paper["decision"] is None
    assert paper["forum_id"] is None
    assert paper["created_at_source"] == "2025-11-03T00:00:00"
    assert paper["updated_at_source"] == "2026-10-18T09:31:52.927876"
    assert paper["raw_data_path"] is None


def test_normalized_paper_actually_satisfies_upsert_paper(tmp_path, monkeypatch):
    """Integration: the dict must round-trip through the real DB upsert, not just look right."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "papers.sqlite")
    monkeypatch.setattr(db, "DATA_ROOT", tmp_path)
    provider = OpenAlexProvider(http=FakeHttp())
    paper = provider.normalize(
        oa_work(inverted={"Graph": [0], "learning": [1], "works": [2]}), "neurips", 2024
    )
    db.init_db()
    with db.connect() as con:
        db.put_venue(
            con, venue_id="neurips", name="NeurIPS", year=2024, provider="openalex",
            source_venue_id=OPENALEX_SOURCES["neurips"], kind="conference",
            source_url=None, availability="available",
        )
        assert db.upsert_paper(con, paper, "neurips", 2024) == "inserted"
        # Re-harvest must update, never duplicate: ids are deterministic.
        assert db.upsert_paper(con, paper, "neurips", 2024) == "updated"
        row = con.execute("SELECT * FROM papers").fetchone()
        assert con.execute("SELECT count(*) FROM papers").fetchone()[0] == 1
        assert row["title"] == "A Study of Graph Things"
        assert row["abstract"] == "Graph learning works"
        assert row["source"] == "openalex"
        # FTS5 must have been populated from the normalized fields.
        assert con.execute(
            "SELECT count(*) FROM papers_fts WHERE papers_fts MATCH 'learning'"
        ).fetchone()[0] == 1


def test_normalize_recovers_arxiv_id_from_datacite_doi():
    provider = OpenAlexProvider(http=FakeHttp())
    paper = provider.normalize(
        oa_work(doi="https://doi.org/10.48550/arXiv.2401.00001v2"), "neurips", 2024
    )
    assert paper["doi"] == "10.48550/arXiv.2401.00001v2"
    assert paper["arxiv_id"] == "2401.00001v2"


def test_normalize_prefers_publisher_pdf_over_open_access_url():
    provider = OpenAlexProvider(http=FakeHttp())
    work = oa_work(
        primary_location={
            "landing_page_url": "https://proceedings.example/paper",
            "pdf_url": "https://proceedings.example/paper.pdf",
        },
        open_access={"is_oa": True, "oa_url": "https://arxiv.org/pdf/2405.14458"},
    )
    assert provider.normalize(work, "neurips", 2024)["pdf_url"] == (
        "https://proceedings.example/paper.pdf"
    )


def test_normalize_accepts_arxiv_pdf_path_as_open_access_pdf():
    """arXiv serves PDFs at /pdf/<id> with no .pdf suffix; that is still a PDF."""
    provider = OpenAlexProvider(http=FakeHttp())
    paper = provider.normalize(
        oa_work(
            primary_location={"landing_page_url": None, "pdf_url": None},
            open_access={"is_oa": True, "oa_url": "https://arxiv.org/pdf/2405.14458"},
        ),
        "neurips", 2024,
    )
    assert paper["pdf_url"] == "https://arxiv.org/pdf/2405.14458"


def test_normalize_rejects_landing_page_as_open_access_pdf():
    """A repository *landing* page is not a PDF and must not be stored as one."""
    provider = OpenAlexProvider(http=FakeHttp())
    for oa_url in ("https://example.org/abs/1234", "https://doi.org/10.1/xyz"):
        paper = provider.normalize(
            oa_work(
                primary_location={"landing_page_url": None, "pdf_url": None},
                open_access={"is_oa": True, "oa_url": oa_url},
            ),
            "neurips", 2024,
        )
        assert paper["pdf_url"] is None, oa_url


def test_normalize_uses_raw_author_name_when_author_entity_unresolved():
    provider = OpenAlexProvider(http=FakeHttp())
    paper = provider.normalize(
        oa_work(
            authorships=[
                {"author": {"display_name": None}, "raw_author_name": "Someone Unknown"},
                {"author": {"display_name": "Known Person"}},
                {"author": {}, "raw_author_name": "   "},
                "not-a-mapping",
            ]
        ),
        "neurips", 2024,
    )
    assert paper["authors"] == ["Someone Unknown", "Known Person"]


# --------------------------------------------------------------------------- #
# (c) pagination + limit                                                      #
# --------------------------------------------------------------------------- #
def test_search_source_paginates_and_stops_at_end_of_cursor():
    fake = FakeHttp(
        [
            oa_page(["P1", "P2"], next_cursor="CURSOR_2", id_offset=0),
            oa_page(["P3"], next_cursor="CURSOR_3", id_offset=10),
            oa_page(["P4"], next_cursor=None, id_offset=20),  # null cursor == end
        ]
    )
    provider = OpenAlexProvider(http=fake, per_page=2)
    papers = provider.search_source("neurips", 2024)

    assert [p["title"] for p in papers] == ["P1", "P2", "P3", "P4"]
    assert len(fake.calls) == 3
    assert provider.stats.pages == 3
    # First request starts at the documented "*" cursor, then follows next_cursor.
    assert fake.calls[0][1]["cursor"] == "*"
    assert fake.calls[1][1]["cursor"] == "CURSOR_2"
    assert fake.calls[2][1]["cursor"] == "CURSOR_3"
    # The filter must use locations.source.id -- primary_location silently returns 0.
    assert fake.calls[0][1]["filter"] == (
        f"locations.source.id:{OPENALEX_SOURCES['neurips']},publication_year:2024"
    )
    assert fake.calls[0][1]["select"] == OPENALEX_SELECT
    assert fake.calls[0][1]["per-page"] == 2
    # NOTE: ``mailto`` is added by PoliteHttp, which FakeHttp replaces; it is
    # asserted against a real transport in
    # test_requests_always_carry_polite_headers_and_mailto.


def test_search_source_respects_limit_and_shrinks_final_page():
    """limit=3 over per_page=2 pages must issue exactly 2 requests: 2 + 1.

    Over-fetching the last page would waste a whole page of abstracts for a
    1-record remainder.
    """
    fake = FakeHttp(
        [
            oa_page(["P1", "P2"], next_cursor="C2", id_offset=0),
            oa_page(["P3", "P4"], next_cursor="C3", id_offset=10),
            oa_page(["P5"], next_cursor="C4", id_offset=20),
        ]
    )
    provider = OpenAlexProvider(http=fake, per_page=2)
    papers = provider.search_source("neurips", 2024, limit=3)

    assert [p["title"] for p in papers] == ["P1", "P2", "P3"]
    assert len(fake.calls) == 2
    assert fake.calls[0][1]["per-page"] == 2
    assert fake.calls[1][1]["per-page"] == 1  # only the remainder requested
    assert provider.stats.returned == 3


def test_search_source_limit_zero_short_circuits_without_requests():
    fake = FakeHttp([oa_page(["P1"])])
    provider = OpenAlexProvider(http=fake)
    assert provider.search_source("neurips", 2024, limit=0) == []
    assert fake.calls == []


def test_search_source_stops_when_cursor_repeats():
    """A repeated cursor is a server bug; stop instead of looping forever."""
    fake = FakeHttp(
        [
            oa_page(["P1"], next_cursor="SAME", id_offset=0),
            oa_page(["P2"], next_cursor="SAME", id_offset=10),
            oa_page(["P3"], next_cursor="SAME", id_offset=20),
        ]
    )
    provider = OpenAlexProvider(http=fake, per_page=1)
    papers = provider.search_source("neurips", 2024)
    assert [p["title"] for p in papers] == ["P1", "P2"]
    assert len(fake.calls) == 2


def test_search_source_deduplicates_repeated_work_ids():
    """Overlapping pages must not produce duplicate primary keys.

    Cursor paging can legitimately return the same work twice (a record updated
    mid-harvest shifts the page boundary). ``papers.id`` is a primary key, so a
    repeat must be dropped here rather than surfacing as an upsert collision.
    """
    duplicate = {"id": "https://openalex.org/W0", "title": "Seen Twice"}
    fake = FakeHttp(
        [
            {"meta": {"next_cursor": "C2"}, "results": [duplicate, dict(duplicate)]},
            {"meta": {"next_cursor": None}, "results": [dict(duplicate),
                                                        {"id": "https://openalex.org/W99",
                                                         "title": "Fresh"}]},
        ]
    )
    provider = OpenAlexProvider(http=fake, per_page=2)
    papers = provider.search_source("neurips", 2024)

    assert [p["title"] for p in papers] == ["Seen Twice", "Fresh"]
    assert provider.stats.duplicates == 2  # once within page 1, once across pages
    assert provider.stats.returned == 2


def test_provider_satisfies_harvest_orchestrator_protocol():
    """The class must drop into ``MultiSourceHarvester`` with no adapter.

    ``harvest`` is duck-typed (``name`` + ``harvest(venue, year, limit=...)``);
    this asserts against the real orchestrator so the claim in the module
    docstring cannot rot.
    """
    from paper_ingest.harvest import MultiSourceHarvester

    fake = FakeHttp([oa_page(["Harvested Paper"], next_cursor=None)])
    provider = OpenAlexProvider(http=fake, per_page=200)

    assert provider.name == "openalex"
    result = MultiSourceHarvester([provider]).harvest("neurips", 2024, limit=10)

    assert [p["title"] for p in result.papers] == ["Harvested Paper"]
    assert result.attempts[0].provider == "openalex"
    assert result.attempts[0].status == "ok"
    assert result.summary()["with_abstract"] == 0  # oa_page sets no abstract


def test_search_source_rejects_unknown_venue():
    provider = OpenAlexProvider(http=FakeHttp())
    with pytest.raises(ValueError, match="no OpenAlex source id"):
        provider.search_source("neurips_2024", 2024)


def test_build_filter_never_uses_primary_location():
    """Regression guard for the exact bug that produced silently empty harvests."""
    provider = OpenAlexProvider(http=FakeHttp())
    for venue in OPENALEX_SOURCES:
        built = provider.build_filter(venue, 2024)
        assert built.startswith("locations.source.id:")
        assert "primary_location" not in built


def test_low_coverage_venues_are_declared():
    assert OPENALEX_LOW_COVERAGE_VENUES == frozenset({"icml", "iclr"})
    # Their ids are still configured (DOI lookups work) but flagged as low.
    assert OPENALEX_SOURCES["icml"] == "S4306419644"
    assert OPENALEX_SOURCES["iclr"] == "S4306419637"


# --------------------------------------------------------------------------- #
# (d) 429 retry                                                               #
# --------------------------------------------------------------------------- #
def make_transport(responses):
    """httpx transport that replays scripted responses and records requests."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if not responses:
            raise AssertionError("unexpected extra request")
        status, headers, body = responses.pop(0)
        return httpx.Response(status, headers=headers, json=body)

    return httpx.MockTransport(handler), calls


def ok_payload(title="Retried Paper"):
    return {"meta": {"next_cursor": None}, "results": [{"id": "https://openalex.org/W1", "title": title}]}


def test_429_is_retried_then_succeeds():
    """429 -> sleep -> 200 must return data, and the Retry-After must be honoured."""
    transport, calls = make_transport(
        [
            (429, {"Retry-After": "7"}, {"error": "rate limited"}),
            (200, {}, ok_payload()),
        ]
    )
    sleeps: list[float] = []
    with httpx.Client(transport=transport) as client:
        http = PoliteHttp(client=client, mailto="test@example.org", sleep=sleeps.append)
        payload = http.get_json("https://api.openalex.org/works", {"filter": "x"})

    assert payload["results"][0]["title"] == "Retried Paper"
    assert len(calls) == 2
    assert sleeps == [7.0]  # Retry-After wins over exponential backoff
    assert http.stats.rate_limited == 1
    assert http.stats.retries == 1
    assert http.stats.requests == 2


def test_retry_after_is_capped_so_a_huge_value_cannot_stall_a_sync():
    transport, _ = make_transport(
        [
            (429, {"Retry-After": "99999"}, {}),
            (200, {}, ok_payload()),
        ]
    )
    sleeps: list[float] = []
    with httpx.Client(transport=transport) as client:
        http = PoliteHttp(
            client=client, sleep=sleeps.append, backoff_max=30.0, mailto="test@example.org"
        )
        http.get_json("https://api.openalex.org/works")
    assert sleeps == [30.0]


def test_exponential_backoff_without_retry_after():
    transport, calls = make_transport(
        [
            (500, {}, {}),
            (503, {}, {}),
            (200, {}, ok_payload()),
        ]
    )
    sleeps: list[float] = []
    with httpx.Client(transport=transport) as client:
        http = PoliteHttp(
            client=client, sleep=sleeps.append, backoff_base=1.0, backoff_max=60.0,
            mailto="test@example.org",
        )
        http.get_json("https://api.crossref.org/works")
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]
    assert http.stats.server_errors == 2


def test_exhausted_retries_raise_described_error():
    transport, calls = make_transport([(429, {}, {}) for _ in range(3)])
    sleeps: list[float] = []
    with httpx.Client(transport=transport) as client:
        http = PoliteHttp(client=client, sleep=sleeps.append, max_attempts=3,
                          mailto="test@example.org")
        with pytest.raises(PoliteHttpError) as excinfo:
            http.get_json("https://api.openalex.org/works")
    assert excinfo.value.status_code == 429
    assert len(calls) == 3
    # Exhaustion must not be silently reported as "no results".


def test_client_errors_are_not_retried():
    """400 means our query is wrong; repeating it would just burn the budget."""
    transport, calls = make_transport([(400, {}, {"error": "Invalid query parameters"})])
    with httpx.Client(transport=transport) as client:
        http = PoliteHttp(client=client, sleep=lambda _s: None, mailto="test@example.org")
        with pytest.raises(PoliteHttpError) as excinfo:
            http.get_json("https://api.openalex.org/works", {"select": "bogus"})
    assert excinfo.value.status_code == 400
    assert len(calls) == 1


def test_404_returns_none_instead_of_raising():
    transport, _ = make_transport([(404, {}, {"error": "not found"})])
    with httpx.Client(transport=transport) as client:
        http = PoliteHttp(client=client, sleep=lambda _s: None, mailto="test@example.org")
        assert http.get_json("https://api.openalex.org/works/doi:10.1/nope") is None
    assert http.stats.not_found == 1


def test_requests_always_carry_polite_headers_and_mailto():
    """Politeness is a functional requirement: it is what grants the higher limit."""
    transport, calls = make_transport([(200, {}, {"results": []})])
    with httpx.Client(transport=transport) as client:
        http = PoliteHttp(client=client, sleep=lambda _s: None, mailto="someone@university.edu")
        http.get_json("https://api.openalex.org/works")
    request = calls[0]
    assert request.headers["User-Agent"] == user_agent("someone@university.edu")
    assert "mailto:someone@university.edu" in request.headers["User-Agent"]
    assert request.url.params["mailto"] == "someone@university.edu"


def test_get_by_doi_returns_none_for_unknown_doi_and_strips_prefix():
    fake = FakeHttp([None])
    provider = OpenAlexProvider(http=fake)
    assert provider.get_by_doi("https://doi.org/10.9999/nonexistent") is None
    assert fake.calls[0][0] == "https://api.openalex.org/works/doi:10.9999/nonexistent"


def test_get_by_doi_normalizes_a_real_work():
    fake = FakeHttp([oa_work(inverted={"Hello": [0], "world": [1]})])
    provider = OpenAlexProvider(http=fake)
    paper = provider.get_by_doi("10.52202/079017-3429")
    assert paper["id"] == "openalex:W4415795831"
    assert paper["abstract"] == "Hello world"


def test_get_by_doi_rejects_empty_input_without_a_request():
    fake = FakeHttp([])
    provider = OpenAlexProvider(http=fake)
    assert provider.get_by_doi("   ") is None
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# (e) Crossref JATS cleaning                                                  #
# --------------------------------------------------------------------------- #
def test_crossref_jats_abstract_is_cleaned_to_plain_text():
    raw = (
        '<jats:title>Abstract</jats:title>\n'
        '<jats:p xml:lang="en">We introduce <jats:italic>FLAPS</jats:italic>, a method '
        "that improves accuracy &amp; cuts latency below 10 ms.</jats:p>"
    )
    cleaned = clean_markup_text(raw)
    assert cleaned == (
        "We introduce FLAPS, a method that improves accuracy & cuts latency below 10 ms."
    )
    assert "<jats" not in cleaned
    assert "Abstract" not in cleaned.split(".")[0]


def test_clean_markup_text_does_not_apply_tags_to_escaped_entity_markup():
    """A naive strip-then-unescape leaves "&lt;10ms" in the text; this order fixes it."""
    assert clean_markup_text("<jats:p>below &lt;10ms and &amp; more</jats:p>") == (
        "below <10ms and & more"
    )
    # Double-encoded tags (seen in some deposits) must also collapse to text.
    assert clean_markup_text("&lt;jats:p&gt;Real text here&lt;/jats:p&gt;") == "Real text here"


@pytest.mark.parametrize("value", [None, "", "   ", 42, ["<jats:p>x</jats:p>"]])
def test_clean_markup_text_returns_none_for_blank_or_non_string(value):
    assert clean_markup_text(value) is None


def test_crossref_normalize_cleans_abstract_and_maps_db_fields():
    provider = CrossrefProvider(http=FakeHttp())
    record = {
        "DOI": "10.1038/s42256-024-00798-7",
        "title": ["A Crossref Paper"],
        "abstract": '<jats:title>Abstract</jats:title><jats:p>Deep learning &amp; imaging.</jats:p>',
        "author": [
            {"given": "Ada", "family": "Lovelace"},
            {"name": "The Consortium"},
            {"given": "OnlyGiven"},
        ],
        "subject": ["Machine Learning", "machine learning", "Imaging"],
        "issued": {"date-parts": [[2024, 5, 1]]},
        "created": {"date-time": "2024-06-01T10:00:00Z"},
        "deposited": {"date-time": "2026-06-19T15:47:20Z"},
        "type": "journal-article",
        "URL": "https://doi.org/10.1038/s42256-024-00798-7",
        "container-title": ["Nature Machine Intelligence"],
        "link": [
            {"URL": "https://example.org/full.xml", "content-type": "text/xml"},
            {"URL": "https://example.org/paper.pdf", "content-type": "application/pdf"},
        ],
    }
    paper = provider.normalize(record, "neurips", 2024)

    assert REQUIRED_PAPER_KEYS <= set(paper)
    assert paper["id"] == "crossref:10.1038/s42256-024-00798-7"
    assert paper["source"] == "crossref"
    assert paper["source_paper_id"] == "10.1038/s42256-024-00798-7"
    assert paper["abstract"] == "Deep learning & imaging."
    assert paper["authors"] == ["Ada Lovelace", "The Consortium", "OnlyGiven"]
    assert paper["keywords"] == ["Machine Learning", "Imaging"]  # case-insensitive dedupe
    assert paper["doi"] == "10.1038/s42256-024-00798-7"
    assert paper["publication_year"] == 2024
    assert paper["submission_year"] == 2024
    assert paper["pdf_url"] == "https://example.org/paper.pdf"  # XML link skipped
    assert paper["paper_url"] == "https://doi.org/10.1038/s42256-024-00798-7"
    assert paper["created_at_source"] == "2024-06-01T10:00:00Z"
    assert paper["updated_at_source"] == "2026-06-19T15:47:20Z"
    assert paper["status"] == "published"
    assert paper["primary_area"] is None
    assert paper["container_title"] == "Nature Machine Intelligence"


def test_crossref_normalize_rejects_record_without_doi():
    provider = CrossrefProvider(http=FakeHttp())
    with pytest.raises(MalformedWorkError, match="no DOI"):
        provider.normalize({"title": ["No DOI here"]}, "neurips", 2024)


def test_crossref_normalized_paper_satisfies_upsert_paper(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "papers.sqlite")
    monkeypatch.setattr(db, "DATA_ROOT", tmp_path)
    provider = CrossrefProvider(http=FakeHttp())
    paper = provider.normalize(
        {
            "DOI": "10.52202/079017-4242",
            "title": ["Treatment of Statistical Estimation Problems"],
            "abstract": "<jats:p>Randomized smoothing &amp; more.</jats:p>",
            "author": [{"given": "Vaclav", "family": "Voracek"}],
            "issued": {"date-parts": [[2024]]},
        },
        "neurips",
        2024,
    )
    db.init_db()
    with db.connect() as con:
        db.put_venue(
            con, venue_id="neurips", name="NeurIPS", year=2024, provider="crossref",
            source_venue_id=None, kind="conference", source_url=None, availability="available",
        )
        assert db.upsert_paper(con, paper, "neurips", 2024) == "inserted"
        row = con.execute("SELECT abstract,doi FROM papers").fetchone()
        assert row["abstract"] == "Randomized smoothing & more."
        assert row["doi"] == "10.52202/079017-4242"


# --------------------------------------------------------------------------- #
# (f) malformed work is skipped, not raised                                   #
# --------------------------------------------------------------------------- #
def test_malformed_work_is_skipped_not_raised():
    """A page with two unusable records still yields the good ones."""
    bad_untitled = {"id": "https://openalex.org/W9", "title": None}
    bad_title_only_spaces = {"id": "https://openalex.org/W8", "title": "   "}
    good = oa_work("W10", title="Only Good Paper", inverted={"ok": [0]})
    not_a_mapping = "garbage"

    fake = FakeHttp(
        [{"meta": {"next_cursor": None}, "results": [bad_untitled, good, not_a_mapping, bad_title_only_spaces]}]
    )
    provider = OpenAlexProvider(http=fake)
    papers = provider.search_source("neurips", 2024)

    assert [p["title"] for p in papers] == ["Only Good Paper"]
    assert provider.stats.fetched == 4
    assert provider.stats.skipped == 3
    assert provider.stats.returned == 1


def test_normalize_raises_malformed_for_unusable_records():
    """The skip above relies on a clear exception type, not a bare KeyError."""
    provider = OpenAlexProvider(http=FakeHttp())
    with pytest.raises(MalformedWorkError):
        provider.normalize({"id": "https://openalex.org/W1", "title": None}, "neurips", 2024)
    with pytest.raises(MalformedWorkError):
        provider.normalize("not-a-mapping", "neurips", 2024)


def test_search_source_survives_empty_and_non_mapping_payloads():
    provider = OpenAlexProvider(http=FakeHttp([None]))  # 404 -> None
    assert provider.search_source("neurips", 2024) == []

    provider2 = OpenAlexProvider(http=FakeHttp([{"meta": {}, "results": []}]))
    assert provider2.search_source("neurips", 2024) == []

    provider3 = OpenAlexProvider(http=FakeHttp([{"results": [None, 5]}]))
    assert provider3.search_source("neurips", 2024) == []
    assert provider3.stats.skipped == 2


# --------------------------------------------------------------------------- #
# Crossref container post-filter                                              #
# --------------------------------------------------------------------------- #
def test_container_relevance_separates_real_venue_from_same_word_journals():
    """Threshold is calibrated on live data: real=1.00/0.80, noise=0.40."""
    target = "Advances in Neural Information Processing Systems"
    assert container_relevance(target + " 37", target) == 1.0
    assert container_relevance(
        "Proceedings of the 37th International Conference on Neural Information Processing Systems",
        target,
    ) >= CONTAINER_RELEVANCE_THRESHOLD
    for noise in ("Quantum Information Processing", "Information Processing Letters"):
        assert container_relevance(noise, target) < CONTAINER_RELEVANCE_THRESHOLD


def test_search_container_filters_out_same_named_venues():
    items = [
        {"DOI": "10.1/keep1", "title": ["Real"], "container-title": ["Advances in Neural Information Processing Systems 37"]},
        {"DOI": "10.1/drop1", "title": ["Noise"], "container-title": ["Information Processing Letters"]},
        {"DOI": "10.1/keep2", "title": ["Also real"], "container-title": ["Advances in Neural Information Processing Systems"]},
    ]
    fake = FakeHttp([{"message": {"total-results": 3, "items": items}}])
    provider = CrossrefProvider(http=fake)
    kept = provider.search_container("Advances in Neural Information Processing Systems", 2024, rows=10)

    assert [r["DOI"] for r in kept] == ["10.1/keep1", "10.1/keep2"]
    assert provider.stats.skipped == 1
    assert provider.stats.returned == 2
    params = fake.calls[0][1]
    assert params["query.container-title"] == "Advances in Neural Information Processing Systems"
    assert params["filter"] == "from-pub-date:2024-01-01,until-pub-date:2024-12-31"
    assert params["select"] == CROSSREF_SELECT
    # offset paging, NOT cursor: cursor mode drops the relevance ranking.
    assert params["offset"] == 0
    assert "cursor" not in params


def test_search_container_pages_by_offset_and_respects_rows():
    page = [
        {"DOI": f"10.1/{i}", "title": ["t"], "container-title": ["Advances in Neural Information Processing Systems"]}
        for i in range(2)
    ]
    fake = FakeHttp(
        [
            {"message": {"total-results": 4, "items": list(page)}},
            {"message": {"total-results": 4, "items": [
                {"DOI": "10.1/2", "title": ["t"], "container-title": ["Advances in Neural Information Processing Systems"]},
                {"DOI": "10.1/3", "title": ["t"], "container-title": ["Advances in Neural Information Processing Systems"]},
            ]}},
        ]
    )
    provider = CrossrefProvider(http=fake)
    # page_size is independent of rows: without that separation a 2-item page is
    # a "short page" and the second page would be unreachable.
    kept = provider.search_container(
        "Advances in Neural Information Processing Systems", 2024, rows=4, page_size=2
    )
    assert [r["DOI"] for r in kept] == ["10.1/0", "10.1/1", "10.1/2", "10.1/3"]
    assert fake.calls[1][1]["offset"] == 2
    assert fake.calls[0][1]["rows"] == 2


def test_crossref_get_by_doi_handles_prefixed_and_missing_dois():
    fake = FakeHttp([{"message": {"DOI": "10.1/abc", "title": ["X"]}}, None])
    provider = CrossrefProvider(http=fake)

    record = provider.get_by_doi("https://doi.org/10.1/ABC")
    assert record["DOI"] == "10.1/abc"
    assert fake.calls[0][0] == "https://api.crossref.org/works/10.1/ABC"

    assert provider.get_by_doi("10.1/missing") is None
    assert provider.get_by_doi("") is None
    assert len(fake.calls) == 2  # the empty DOI never hit the network


# --------------------------------------------------------------------------- #
# Shared helpers                                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://doi.org/10.1234/ABC", "10.1234/ABC"),
        ("http://dx.doi.org/10.1234/ABC", "10.1234/ABC"),
        ("doi: 10.1234/ABC", "10.1234/ABC"),
        ("10.1234/ABC", "10.1234/ABC"),
        ("  ", None),
        (None, None),
        (1234, None),
    ],
)
def test_normalize_doi(value, expected):
    assert normalize_doi(value) == expected


def test_user_agent_is_honest_and_carries_contact():
    agent = user_agent("someone@example.org")
    assert "PaperGraph/0.2" in agent
    assert "mailto:someone@example.org" in agent
    # Not a browser impersonation: no Mozilla/Chrome token.
    assert "Mozilla" not in agent
    assert "Chrome" not in agent


@pytest.mark.parametrize(
    "value, expected",
    [(None, DEFAULT_MAILTO), ("", DEFAULT_MAILTO), ("   ", DEFAULT_MAILTO)],
)
def test_blank_mailto_falls_back_to_default(value, expected):
    """A blank contact would silently fail the polite-pool requirement."""
    assert PoliteHttp(mailto=value).mailto == expected


def test_provider_closes_only_clients_it_owns():
    """An injected client belongs to the caller; closing it would break reuse."""
    fake = FakeHttp()
    provider = OpenAlexProvider(http=fake)
    provider.close()
    assert fake.closed is True

    transport, _ = make_transport([(200, {}, {})])
    client = httpx.Client(transport=transport)
    try:
        http = PoliteHttp(client=client, mailto="test@example.org")
        http.close()
        assert not client.is_closed  # still usable by its owner
    finally:
        client.close()


def test_concurrency_is_capped():
    """The cap is what makes several venue-years a polite client, not a flood."""
    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.02)  # hold the slot so overlap is observable
        with lock:
            in_flight -= 1
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        http = PoliteHttp(client=client, mailto="test@example.org", max_concurrency=2)
        threads = [
            threading.Thread(target=http.get_json, args=("https://api.openalex.org/works",))
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert peak <= 2, f"cap violated: {peak} concurrent requests"
    assert http.stats.requests == 8


def test_logs_do_not_contain_the_contact_address():
    """Sync logs are shared; the query string carries the mailto and must be dropped."""
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("test.providers_openalex.redaction")
    logger.setLevel(logging.DEBUG)
    logger.handlers = [Capture()]
    logger.propagate = False

    transport, _ = make_transport([(500, {}, {}), (429, {"Retry-After": "0"}, {}),
                                   (200, {}, ok_payload())])
    with httpx.Client(transport=transport) as client:
        http = PoliteHttp(
            client=client, sleep=lambda _s: None, mailto="secret.person@university.edu",
            logger=logger,
        )
        http.get_json("https://api.openalex.org/works", {"filter": "x"})

    assert records, "expected retry logging to happen"
    joined = " ".join(records)
    assert "secret.person@university.edu" not in joined
    # Useful content survives redaction: the host/path and the status are logged.
    assert "https://api.openalex.org/works" in joined
    assert "500" in joined
