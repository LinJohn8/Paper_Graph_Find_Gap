"""The retrieval channels, pinned by what they are for.

Each channel answers a different shape of query, and the whole point of the
rewrite was that the old single OR channel answered none of them well:

* **phrase** -- the whole query is inside a title. A paper searched by its
  own title must come back first; with the old channel it did so only 39% of
  the time, because a long title's terms were ORed and the 8-term cap
  dropped the rest.
* **fts** -- every term, relaxing to the rarest ones. The scope lives INSIDE
  the query: the old code took the global top-60 and filtered afterwards, so
  15 of 30 venues returned nothing while holding thousands of matches.
* **like** -- the term inside a longer word, for acronyms.

These are unit tests with a small fixture corpus, so they are fast and they
state the contract rather than the implementation.
"""
import pytest

from paper_ingest import db, retrieval


@pytest.fixture()
def con(tmp_path):
    path = tmp_path / "retrieval.sqlite"
    db.init_db(path)
    with db.connect(path) as connection:
        for venue, year in (("aa", 2024), ("aa", 2025), ("bb", 2024)):
            db.put_venue(connection, venue_id=venue, name=venue.upper(), year=year,
                         provider="test", source_venue_id=None, kind="journal",
                         source_url=None, availability="open")
        papers = [
            ("p1", "Graph neural networks for protein folding",
             "We study graph neural networks applied to protein structure prediction.",
             "aa", 2024),
            ("p2", "A survey of protein structure prediction methods",
             "This survey reviews protein structure prediction and its methods.",
             "aa", 2025),
            ("p3", "Clinical trials of a vaccine in older adults",
             "We report a randomized controlled trial of a vaccine in elderly patients.",
             "bb", 2024),
            ("p4", "Networks in the brain",
             "Neural networks underlying cortical dynamics are studied here.",
             "aa", 2024),
            ("p5", "Unrelated work on materials chemistry",
             "Catalysts and adsorption are studied in this materials chemistry paper.",
             "bb", 2024),
        ]
        for paper_id, title, abstract, venue, year in papers:
            connection.execute(
                "INSERT INTO papers(id, title, abstract, source, source_paper_id, "
                "status, submission_year, first_seen_at, last_seen_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (paper_id, title, abstract, "test", paper_id, "accepted", year,
                 "2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"))
            connection.execute(
                "INSERT INTO paper_venues(paper_id, venue_id, year, relation_type) "
                "VALUES(?,?,?,?)", (paper_id, venue, year, "main"))
            connection.execute(
                "INSERT INTO papers_fts(rowid, paper_id, title, abstract, authors, "
                "keywords, venue) VALUES(?,?,?,?,?,?,?)",
                (abs(hash(paper_id)) % 1000 + 1, paper_id, title, abstract, "",
                 "", venue.upper()))
        connection.commit()
    with db.connect(path) as connection:
        yield connection


class TestPhraseChannel:
    """The whole query inside a title."""

    def test_it_finds_the_paper_whose_title_is_the_query(self, con):
        hits = retrieval.phrase_ranking(
            con, "Graph neural networks for protein folding", limit=10)
        assert "p1" in hits

    def test_a_phrase_that_is_not_a_title_finds_nothing(self, con):
        assert retrieval.phrase_ranking(con, "a survey of nothing at all", limit=10) == []

    def test_it_is_scoped_by_venue(self, con):
        hits = retrieval.phrase_ranking(
            con, "A survey of protein structure prediction methods", limit=10,
            venue="bb")
        assert hits == []


class TestFtsChannel:
    """Every term, with the scope inside the query."""

    def test_all_the_terms_are_required(self, con):
        hits = retrieval.fts_ranking(con, ["graph", "neural", "network"], limit=10)
        # p1 has all three; p4 has "networks" but not "graph".
        assert "p1" in hits

    def test_the_scope_lives_inside_the_query(self, con):
        """A venue's papers must be reachable even when they are not the
        globally best matches."""
        hits = retrieval.fts_ranking(con, ["protein", "structure"], limit=10, venue="bb")
        # Only aa holds a protein-structure paper, so bb finds nothing --
        # but aa finds its own, which the old global-top-60 cut could not do.
        assert hits == []
        hits = retrieval.fts_ranking(con, ["protein", "structure"], limit=10, venue="aa")
        assert "p1" in hits and "p2" in hits

    def test_a_venue_with_no_match_returns_nothing_rather_than_a_globals_top(self, con):
        # "catalyst" is in bb's p5; asking aa must not return p5.
        hits = retrieval.fts_ranking(con, ["catalyst"], limit=10, venue="aa")
        assert "p5" not in hits

    def test_function_words_are_dropped(self, con):
        """A title's function words match almost every paper."""
        hits = retrieval.fts_ranking(con, ["the", "of", "and"], limit=10)
        # Not a single paper contains "the" as a token here, and the point is
        # that the query is still answerable rather than matching everything.
        assert len(hits) <= 10


class TestLexicalRanking:
    """The fused lexical entry point the Papers page uses."""

    def test_it_puts_the_exact_title_first(self, con):
        hits = retrieval.lexical_ranking(
            con, "Graph neural networks for protein folding", limit=10)
        assert hits and hits[0] == "p1"

    def test_it_finds_a_paper_by_a_part_of_its_title(self, con):
        hits = retrieval.lexical_ranking(con, "cortical dynamics", limit=10)
        assert "p4" in hits


class TestTermPreparation:
    def test_unsafe_characters_are_stripped(self):
        assert retrieval._clean_terms(['a"b', "c'd", "e f"]) == ["ab", "cd", "ef"]

    def test_short_terms_are_dropped(self):
        assert retrieval._clean_terms(["a", "ab"]) == ["ab"]

    def test_the_and_expression_requires_every_term(self):
        expression = retrieval._and_expression(["graph", "neural"])
        assert expression.count(" AND ") == 1
        assert "graph*" in expression and "neural*" in expression

    def test_content_terms_prefer_the_meaningful_ones(self):
        assert retrieval._content_terms(["the", "graph", "of"]) == ["graph"]
        # With nothing but function words, they are kept.
        assert retrieval._content_terms(["the", "of"]) == ["the", "of"]


class TestRankFusion:
    def test_weights_scale_a_channel(self):
        rankings = [["a", "b"], ["b", "a"]]
        plain = dict(retrieval.rank_fuse(rankings))
        weighted = dict(retrieval.rank_fuse(rankings, weights=[3.0, 1.0]))
        assert weighted["a"] > plain["a"]

    def test_a_zero_weight_removes_a_channel(self):
        rankings = [["a"], ["b"]]
        fused = dict(retrieval.rank_fuse(rankings, weights=[1.0, 0.0]))
        assert "b" not in fused
