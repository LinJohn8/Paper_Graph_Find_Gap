"""Tests for the offline replay harness (``paper_ingest.replay``).

WHY THIS FILE EXISTS
--------------------
The providers in this project parse responses they do not control: OpenAlex
JSON, Europe PMC JSON, jmlr.org HTML, the arXiv Atom feed.  When one of those
services changes shape, the harvest does not fail -- it returns less, and a
silently empty harvest looks exactly like a quiet year.

The tests below therefore do two things:

1. they check the harness itself (matching, the loud failure on a missing
   fixture, the on-disk format), and
2. they push the **recorded** payloads through the **production** parsers --
   ``providers_openalex.rebuild_abstract``/``OpenAlexProvider.normalize``,
   ``jmlr.volume_entries``/``jmlr.fetch_paper``,
   ``journal_abstracts.fetch_europepmc_abstract`` and
   ``provider_arxiv.ArxivProvider.parse_feed``.

Nothing here touches the network: every request is answered from
``tests/fixtures/`` through :class:`paper_ingest.replay.ReplayClient`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_ingest import jmlr, journal_abstracts  # noqa: E402
from paper_ingest.provider_arxiv import ArxivProvider  # noqa: E402
from paper_ingest.providers_openalex import (  # noqa: E402
    OpenAlexProvider,
    rebuild_abstract,
)
from paper_ingest.replay import (  # noqa: E402
    ReplayClient,
    ReplayTransport,
    RecordedResponse,
    fixture_name,
    load_fixture_dir,
    record,
    write_fixture,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

OPENALEX_URL = "https://api.openalex.org/works"
EUROPEPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
JMLR_VOLUME_URL = "https://jmlr.org/papers/v24/"
JMLR_PAPER_URL = "https://jmlr.org/papers/v24/18-080.html"
ARXIV_URL = "http://export.arxiv.org/api/query"


# --------------------------------------------------------------------------- #
# The fixture directory                                                       #
# --------------------------------------------------------------------------- #
def test_fixture_dir_holds_every_recorded_payload():
    """The recordings are found by URL, so a renamed fixture is a loud failure."""
    table = load_fixture_dir(FIXTURES)
    assert (("GET", OPENALEX_URL)) in table
    assert (("GET", EUROPEPMC_URL)) in table
    assert (("GET", JMLR_VOLUME_URL)) in table
    assert (("GET", JMLR_PAPER_URL)) in table
    assert (("GET", ARXIV_URL)) in table
    # The bodies are the payloads themselves, not wrappers around them.
    payload = json.loads(table[("GET", OPENALEX_URL)]["body"])
    assert payload["meta"]["count"] == 2
    assert len(payload["results"]) == 2
    assert "<dl>" in table[("GET", JMLR_VOLUME_URL)]["body"]


def test_fixture_name_follows_the_documented_convention():
    """``<METHOD>__<host>__<path with '/' -> '_'>.json``."""
    assert fixture_name(OPENALEX_URL) == "GET__api.openalex.org__works.json"
    assert fixture_name(JMLR_VOLUME_URL) == "GET__jmlr.org__papers_v24.json"
    assert fixture_name(JMLR_PAPER_URL) == "GET__jmlr.org__papers_v24_18-080.html.json"
    assert fixture_name(OPENALEX_URL, "POST") == "POST__api.openalex.org__works.json"
    # The query string is not part of the name: the exact URL lives in the body.
    assert fixture_name(f"{OPENALEX_URL}?filter=x") == "GET__api.openalex.org__works.json"


def test_record_builds_the_documented_record_shape():
    entry = record(OPENALEX_URL, status=404, headers={"content-type": "text/plain"},
                   body="not found")
    assert entry["url"] == OPENALEX_URL
    assert entry["status"] == 404
    assert entry["headers"] == {"content-type": "text/plain"}
    assert entry["body"] == "not found"


def test_write_fixture_and_load_fixture_dir_round_trip(tmp_path):
    """A recorded response survives the round trip through the fixture dir."""
    body = json.dumps({"meta": {"count": 0, "next_cursor": None}, "results": []})
    path = write_fixture(
        tmp_path,
        OPENALEX_URL,
        headers={"content-type": "application/json"},
        body=body,
    )
    assert path.name == "GET__api.openalex.org__works.json"

    table = load_fixture_dir(tmp_path)
    assert list(table) == [("GET", OPENALEX_URL)]
    entry = table[("GET", OPENALEX_URL)]
    assert entry["status"] == 200
    assert entry["headers"] == {"content-type": "application/json"}
    assert json.loads(entry["body"]) == json.loads(body)


def test_a_malformed_fixture_is_skipped_instead_of_breaking_the_suite(tmp_path, caplog):
    """One broken recording must not take the whole test session down."""
    (tmp_path / "GET__api.openalex.org__works.json").write_text(
        json.dumps(record(OPENALEX_URL, body="{}")), encoding="utf-8"
    )
    broken = tmp_path / "GET__broken.example.org__thing.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not a fixture", encoding="utf-8")

    with caplog.at_level("WARNING", logger="paper_ingest.replay"):
        table = load_fixture_dir(tmp_path)

    assert ("GET", OPENALEX_URL) in table
    assert len(table) == 1
    assert "broken.example.org" in caplog.text


# --------------------------------------------------------------------------- #
# The transport                                                               #
# --------------------------------------------------------------------------- #
def test_transport_serves_the_recorded_response():
    """A recorded request is answered with the recorded bytes."""
    transport = ReplayTransport(directory=FIXTURES)
    response = transport.handle_request(httpx.Request("GET", OPENALEX_URL))
    assert isinstance(response, RecordedResponse)
    assert response.status_code == 200
    assert json.loads(response.text)["meta"]["count"] == 2
    assert response.headers["content-type"] == "application/json; charset=utf-8"


def test_transport_raises_for_an_unknown_url_instead_of_returning_an_empty_page():
    """The whole point of the harness: a missing fixture fails loudly.

    A silent empty body would turn a broken parser into a silently empty
    harvest, which is precisely the failure this project keeps hitting.
    """
    transport = ReplayTransport(directory=FIXTURES)
    with pytest.raises(httpx.ConnectError) as excinfo:
        transport.handle_request(httpx.Request("GET", "https://api.openalex.org/works/W1"))
    assert "api.openalex.org/works/W1" in str(excinfo.value)

    client = ReplayClient(directory=FIXTURES)
    with pytest.raises(httpx.ConnectError):
        client.get("https://api.openalex.org/works/W1")


def test_transport_matches_the_query_string_regardless_of_order():
    """``?b=2&a=1`` and ``?a=1&b=2`` are the same request."""
    transport = ReplayTransport({
        ("GET", "https://example.org/search?a=1&b=2"): record(
            "https://example.org/search?a=1&b=2", body="matched"),
    })
    response = transport.handle_request(
        httpx.Request("GET", "https://example.org/search?b=2&a=1"))
    assert response.text == "matched"


def test_transport_falls_back_to_the_path_when_the_query_differs():
    """A fixture recorded with one query still serves a slightly different one.

    Real clients add parameters that were not in the recording (a polite
    ``mailto``, a pagination cursor, a ``select`` projection).  Demanding an
    exact query match would make every recording useless the moment a
    parameter changed.
    """
    transport = ReplayTransport({
        ("GET", "https://example.org/works?filter=abc"): record(
            "https://example.org/works?filter=abc", body="recorded"),
    })
    response = transport.handle_request(
        httpx.Request("GET", "https://example.org/works?filter=abc&mailto=t@example.org"))
    assert response.text == "recorded"
    # The fallback is per path, not per host.
    with pytest.raises(httpx.ConnectError):
        transport.handle_request(httpx.Request("GET", "https://example.org/other"))


def test_transport_records_which_fixtures_were_used():
    transport = ReplayTransport(directory=FIXTURES)
    client = ReplayClient(transport=transport)
    client.get(OPENALEX_URL, params={"mailto": "test@example.org"})
    client.get(JMLR_VOLUME_URL)
    # ``used`` is what was requested (with its query), ``matched`` is the
    # recording that answered -- the two differ as soon as the client adds a
    # parameter the recording did not have.
    assert transport.used[0][0] == "GET"
    assert transport.used[0][1].startswith(OPENALEX_URL)
    assert transport.used[1] == ("GET", JMLR_VOLUME_URL)
    assert [url for _, url in transport.matched] == [OPENALEX_URL, JMLR_VOLUME_URL]
    assert client.used == transport.used


# --------------------------------------------------------------------------- #
# The recorded response                                                       #
# --------------------------------------------------------------------------- #
def test_recorded_response_json_and_raise_for_status():
    ok = RecordedResponse(
        "https://example.org/x",
        status_code=200,
        text='{"a": 1}',
        headers={"content-type": "application/json"},
    )
    assert ok.json() == {"a": 1}
    assert ok.status_code == 200
    assert ok.text == '{"a": 1}'
    assert ok.content == b'{"a": 1}'
    assert str(ok.url) == "https://example.org/x"
    assert ok.headers["Content-Type"] == "application/json"  # case-insensitive
    assert ok.raise_for_status() is ok

    bad = RecordedResponse("https://example.org/x", status_code=404, text="missing")
    with pytest.raises(httpx.HTTPStatusError):
        bad.raise_for_status()


# --------------------------------------------------------------------------- #
# The production parsers, driven by the recordings                             #
# --------------------------------------------------------------------------- #
def test_openalex_fixture_drives_the_production_parser():
    """The recorded OpenAlex payload goes through the real production code.

    ``rebuild_abstract`` and ``OpenAlexProvider.normalize`` are the functions
    that turn an OpenAlex work into a corpus row.  If OpenAlex renames a
    field or changes the inverted-index format, this test fails here instead
    of silently producing empty abstracts in a live harvest.
    """
    table = load_fixture_dir(FIXTURES)
    payload = json.loads(table[("GET", OPENALEX_URL)]["body"])
    work = payload["results"][0]

    abstract = rebuild_abstract(work["abstract_inverted_index"])
    assert abstract is not None
    assert abstract.startswith("We study the effect of spectral normalization")
    assert "the bound is tight within a factor of two" in abstract
    # The inverted index is a word -> positions map; the reconstruction is a
    # position-ordered sentence, not a bag of words.
    assert abstract.index("We") < abstract.index("Our")

    provider = OpenAlexProvider(mailto="test@example.org")
    try:
        paper = provider.normalize(work, "neurips", 2023)
    finally:
        provider.close()

    assert paper["id"] == "openalex:W4415795831"
    assert paper["title"] == (
        "Generalization Bounds for Spectral Normalization in Large Language Models")
    assert paper["abstract"] == abstract
    assert paper["publication_year"] == 2023
    assert paper["submission_year"] == 2023
    assert paper["authors"] == ["Priya Raman", "Tomas Weiss"]
    assert paper["keywords"] == ["Generalization bounds", "Spectral normalization"]
    assert paper["primary_area"] == "Machine Learning"
    assert paper["doi"] == "10.52202/079017-3429"


def test_openalex_provider_searches_offline_through_the_replay_client():
    """The whole provider (HTTP layer included) runs against the recording.

    This is the end-to-end proof that the harness is usable as a drop-in for
    ``httpx.Client``: the provider builds its own filter/cursor parameters,
    the transport answers them from the recording, and the papers come out
    normalised.
    """
    client = ReplayClient(directory=FIXTURES)
    provider = OpenAlexProvider(mailto="test@example.org", client=client)
    try:
        papers = provider.search_source("neurips", 2023)
    finally:
        provider.close()

    assert len(papers) == 2
    assert papers[0]["id"] == "openalex:W4415795831"
    assert papers[0]["publication_year"] == 2023
    assert papers[0]["authors"] == ["Priya Raman", "Tomas Weiss"]
    assert papers[0]["abstract"].startswith("We study the effect")
    assert papers[1]["id"] == "openalex:W4387120033"
    # The provider asked for a cursor page; the recording answered it.
    assert client.used[0][0] == "GET"
    assert client.used[0][1].startswith(OPENALEX_URL)


def test_jmlr_volume_and_paper_pages_are_parsed_offline(tmp_path, monkeypatch):
    """The recorded jmlr.org pages go through the production JMLR parser.

    ``paper_ingest/jmlr.py`` builds its own client in ``_new_client()``, so
    that factory is the seam the harness exists for: it is monkeypatched to
    hand out a :class:`ReplayClient`.  The module itself is *not* modified --
    a second copy of the parser would defeat the purpose of the recording.
    """
    transport = ReplayTransport(directory=FIXTURES)
    monkeypatch.setattr(jmlr, "_new_client", lambda: ReplayClient(transport=transport))
    # Keep the volume cache inside the test's tmp_path: the production cache
    # lives in the project's data directory and must not be touched.
    monkeypatch.setattr(
        jmlr, "_cache_path", lambda volume: tmp_path / f"volume{volume}.html"
    )

    entries = jmlr.volume_entries(24, force=True)
    assert len(entries) == 2
    first = entries[0]
    assert first["title"] == (
        "Approximation Bounds for Hierarchical Clustering: Average Linkage, "
        "Bisecting K-means, and Local Search")
    assert first["authors"] == ["Benjamin Moseley", "Joshua R. Wang"]
    assert first["year"] == 2023
    assert first["pages"] == "1-36"
    assert first["paper_id"] == "18-080"
    assert first["abs_url"] == JMLR_PAPER_URL
    assert first["pdf_url"] == "https://jmlr.org/papers/volume24/18-080/18-080.pdf"
    assert first["bib_url"] == "https://jmlr.org/papers/v24/18-080.bib"
    # The second entry uses a plain hyphen in the page range, the first the
    # publisher's &minus; entity: both must normalise to "1-36"/"100-120".
    assert entries[1]["pages"] == "100-120"
    assert entries[1]["year"] == 2023

    detail = jmlr.fetch_paper(first["abs_url"])
    assert detail["abstract"].startswith(
        "Hierarchical clustering is a data analysis method")
    assert len(detail["abstract"]) > 200
    assert detail["meta"]["citation_issn"] == "1533-7928"
    assert detail["meta"]["citation_publication_date"] == "2023"
    assert detail["meta"]["citation_journal_title"] == "Journal of Machine Learning Research"

    paper = jmlr._to_paper(first, detail)
    assert paper["id"] == "jmlr:18-080"
    assert paper["title"] == first["title"]
    assert paper["publication_year"] == 2023
    assert paper["abstract"].startswith("Hierarchical clustering")

    # Everything came from the recording; nothing reached the network.
    assert transport.used == [
        ("GET", JMLR_VOLUME_URL),
        ("GET", JMLR_PAPER_URL),
    ]


def test_jmlr_paper_page_without_a_recording_yields_no_abstract(tmp_path, monkeypatch):
    """A missing recording is loud in the harness, fail-soft in jmlr.

    ``jmlr.fetch_paper`` deliberately swallows transport errors so one bad
    page cannot abort a volume -- that is production behaviour and is left
    alone.  The harness itself still raises, which is what a test asserts on.
    """
    transport = ReplayTransport(directory=FIXTURES)
    monkeypatch.setattr(jmlr, "_new_client", lambda: ReplayClient(transport=transport))
    monkeypatch.setattr(
        jmlr, "_cache_path", lambda volume: tmp_path / f"volume{volume}.html"
    )
    # jmlr retries with a backoff; do not actually wait for it.
    monkeypatch.setattr(jmlr.time, "sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(httpx.ConnectError):
        transport.handle_request(httpx.Request("GET", "https://jmlr.org/papers/v99/"))

    detail = jmlr.fetch_paper("https://jmlr.org/papers/v24/19-999.html")
    assert detail == {"abstract": "", "meta": {}}


def test_europepmc_abstract_is_read_offline():
    """The recorded Europe PMC response is parsed by the production helper."""
    client = ReplayClient(directory=FIXTURES)
    abstract = journal_abstracts.fetch_europepmc_abstract(
        client, "10.1038/s41586-023-06004-9")
    assert abstract is not None
    assert abstract.startswith("Mapping the molecular architecture of the human cell")
    assert len(abstract) > 200
    assert "<" not in abstract  # the markers are stripped


def test_arxiv_atom_feed_is_parsed_offline():
    """The recorded Atom feed goes through the production arXiv parser."""
    client = ReplayClient(directory=FIXTURES)
    provider = ArxivProvider(delay=0.0, client=client)
    entries = provider.search("all:diffusion")
    assert len(entries) == 2
    assert entries[0]["arxiv_id"] == "2402.01234"
    assert entries[0]["title"] == "Diffusion Models for Tabular Data: A Survey"
    assert entries[0]["authors"] == ["Jane Roe", "John Doe"]
    assert entries[0]["abstract"].startswith("We survey the rapidly growing literature")
    assert entries[0]["primary_category"] == "cs.LG"

    paper = provider.normalize(entries[0], "icml", 2024)
    assert paper["id"] == "arxiv:2402.01234"
    assert paper["title"] == "Diffusion Models for Tabular Data: A Survey"
    assert paper["publication_year"] == 2024
    assert client.used[0][1].startswith(ARXIV_URL)
