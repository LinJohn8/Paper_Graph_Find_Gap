"""Hybrid retrieval: fuse semantic, lexical and fuzzy channels.

WHY THIS MODULE EXISTS
----------------------
A single-channel retriever fails predictably:

* A TF-IDF/SVD **semantic** ranker returns nothing useful when the query term is
  not in its 1024-word vocabulary. Measured on this corpus: the query ``SECI``
  produced a zero-length query vector, so the ranking was empty and the UI fell
  back to keyword search showing one 0.00-scored row.
* A pure **keyword** (FTS) ranker only sees whole tokens. ``SECI`` is a token in
  exactly one paper; the other two corpus occurrences live inside substrings
  ("Secilia-Cxy", "baseCIL") that FTS cannot match at all.
* Neither channel tells the reader *why* nothing matched or what to try instead.

So retrieval is now three channels fused by Reciprocal Rank Fusion, plus
suggestions:

* **semantic** - cosine in the fitted vector space (what the user usually wants);
* **lexical** - FTS whole-token AND OR-prefix matching (``seci*`` catches "seci",
  "secilm", and hyphenation like "SECI-based");
* **substring** - LIKE over titles (catches usernames, code links and acronyms
  embedded in longer words that tokenisation splits away).

RRF is rank-based, so it needs no score calibration between a cosine (0..1) and
a BM25-ish FTS rank: every channel contributes its ordering only. When the
semantic channel is empty (zero query vector) the other two still answer.

Suggestions power a "did you mean" row: vocabulary terms close to the query
spelling (edit-distance-ish via ``difflib``), so ``SECI`` suggests ``semi``,
``social``, ``series``… and the reader can click one instead of retyping.
"""
from __future__ import annotations

import difflib
import re
import sqlite3
from collections import defaultdict
from typing import Any, Callable, Iterable

from . import db

#: RRF constant (the standard k=60 from Cormack et al.). Larger = flatter
#: influence of each channel's top ranks; 60 is the literature default.
RRF_K = 60

#: bm25 column weights for ``papers_fts(paper_id, title, abstract, authors,
#: keywords, venue)``. A title match says far more about a paper than an
#: abstract match, and bm25 can be told so directly. The paper_id column is
#: UNINDEXED and the venue column holds a display name, so both are weighted
#: zero.
BM25_WEIGHTS = "0.0, 12.0, 1.0, 2.0, 3.0, 0.0"

#: Function words. FTS5's unicode61 tokenizer keeps them, and a title query is
#: full of them -- "Influence of person-organization fit on job satisfaction
#: among preschool teachers" has 24 tokens, 8 of which are function words.
#: They match almost every paper ("the" alone matches 281,482 of 288,262) so
#: they carry no retrieval signal and cost the most to score.
STOPWORDS = frozenset("""
a an the and or of in on at to for with by as is are was were be been being
that this these those it its from into over under between among during
about after before while when where which who whom whose what how why
than then there their they them he she his her our your my me we you i us
do does did done have has had having not no nor so such can could would
should will shall may might must via using use used new novel more most
less least very much many few both each other another same own also
""".split())

#: Term counts are cached per connection: the same query terms come back on
#: every keystroke, and counting a term costs 4-20 ms.
_SELECTIVITY: dict[str, int] = {}


def _clean_terms(terms: Iterable[str]) -> list[str]:
    """Terms safe to interpolate into an FTS MATCH expression.

    FTS5 does not parameterise MATCH, so the expression is built as text and
    must be sanitised: quotes and operators are removed.
    """
    out: list[str] = []
    for term in terms:
        clean = re.sub(r"[^\w\u4e00-\u9fff]", "", str(term))
        if len(clean) >= 2 and clean not in out:
            out.append(clean)
    return out


def _content_terms(terms: list[str]) -> list[str]:
    """The terms worth requiring: function words are dropped when enough
    content terms remain."""
    content = [t for t in terms if t.lower() not in STOPWORDS]
    return content or terms


def _and_expression(terms: list[str]) -> str:
    """``("graph" OR graph*) AND ("neural" OR neural*)``.

    The parenthesised OR is what makes the prefix expansion work: without the
    parentheses FTS5 reads ``"graph" graph*`` as a two-token phrase (a token
    "graph" immediately followed by a token starting with "graph"), which
    matches nothing.
    """
    return " AND ".join(f'("{t}" OR {t}*)' for t in terms)


def _or_expression(terms: list[str]) -> str:
    return " OR ".join(f'"{t}" OR {t}*' for t in terms)


def _selectivity(con: sqlite3.Connection, term: str) -> int:
    """How many papers a term matches -- the cheapest measure of how useful it
    is as a filter. Rare terms narrow; common ones only add noise."""
    cached = _SELECTIVITY.get(term)
    if cached is not None:
        return cached
    try:
        count = con.execute(
            "SELECT count(*) FROM papers_fts WHERE papers_fts MATCH ?",
            (f'"{term}" OR {term}*',)).fetchone()[0]
    except sqlite3.OperationalError:
        count = 0
    _SELECTIVITY[term] = count
    return count


def _fts_query(con: sqlite3.Connection, expression: str, *, limit: int,
               venue: str | None, year: int | None) -> list[str]:
    """Run a MATCH expression, ranked by bm25, with the scope applied INSIDE.

    WHY THE SCOPE IS INSIDE
    -----------------------
    The previous implementation ranked the whole corpus first
    (``ORDER BY bm25 LIMIT 60``) and filtered the survivors by venue on the
    papers side. So a venue-scoped query could only ever return papers that
    were already in the global top 60, and a venue whose papers are not
    globally popular was simply unreachable. Measured on this corpus with the
    query "graph neural network": 15 of 30 venues returned **nothing** while
    holding 2,380, 1,012, 798, 488 ... matching papers. The median venue
    could reach 0.06% of its own matches.

    Filtering inside the query makes the LIMIT apply to the right set, and
    ``bm25()`` works in a join as long as the FTS table stays the outer one
    (the "cannot use bm25 in the requested context" error belongs to a
    GROUP BY, not to a join).
    """
    args: list[Any] = [expression]
    scope = ""
    if venue is not None:
        scope += " AND f.paper_id IN (SELECT paper_id FROM paper_venues WHERE venue_id=?)"
        args.append(venue)
    if year is not None:
        scope += " AND f.paper_id IN (SELECT paper_id FROM paper_venues WHERE year=?)"
        args.append(year)
    sql = (
        "SELECT f.paper_id FROM papers_fts f "
        "WHERE papers_fts MATCH ?" + scope +
        f" ORDER BY bm25(papers_fts, {BM25_WEIGHTS}) LIMIT ?")
    args.append(max(limit, 30))
    try:
        return [row["paper_id"] for row in con.execute(sql, args)]
    except sqlite3.OperationalError:
        return []


def phrase_ranking(con: sqlite3.Connection, raw_query: str, *, limit: int,
                   venue: str | None = None, year: int | None = None) -> list[str]:
    """Papers whose TITLE contains the whole query as a phrase.

    This is the channel that makes a title query work. A title is the most
    precise description a paper has, and FTS5 can match it exactly:
    ``title : "in search of projectively equivariant networks"``.

    Measured on this corpus, a paper searched by its own title came back at
    rank 1 only **39%** of the time with the old lexical channel, because a
    long title's terms were ORed together and the 8-term cap dropped the rest
    -- 93 of the 122 failing queries had more than 8 tokens (median 11).
    The phrase channel does not have that problem: it either matches the
    title or it does not.
    """
    phrase = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff ]+", " ", str(raw_query)).strip()
    phrase = re.sub(r"\s+", " ", phrase)
    if len(phrase) < 8 or len(phrase.split()) < 2:
        return []
    args: list[Any] = [f'title : "{phrase}"']
    scope = ""
    if venue is not None:
        scope += " AND f.paper_id IN (SELECT paper_id FROM paper_venues WHERE venue_id=?)"
        args.append(venue)
    if year is not None:
        scope += " AND f.paper_id IN (SELECT paper_id FROM paper_venues WHERE year=?)"
        args.append(year)
    sql = ("SELECT f.paper_id FROM papers_fts f WHERE papers_fts MATCH ?" + scope +
           " LIMIT ?")
    args.append(max(limit, 30))
    try:
        return [row["paper_id"] for row in con.execute(sql, args)]
    except sqlite3.OperationalError:
        return []


def rank_fuse(rankings: list[list[str]], k: int = RRF_K,
              weights: list[float] | None = None) -> list[tuple[str, float]]:
    """Fuse id rankings by Reciprocal Rank Fusion.

    Each entry in ``rankings`` is a list of paper ids, best first. Returns
    ``(id, fused_score)`` best first. RRF needs no score normalisation between
    channels, which is exactly what a cosine-vs-BM25 fusion needs.

    ``weights`` scales a channel's contribution: RRF has no way to say "this
    channel is exact, that one is a guess", but a weight does. The phrase
    channel -- a title that contains the whole query -- is weighted higher
    than the OR channel, which matches a paper on one common word.
    """
    scores: dict[str, float] = defaultdict(float)
    for index, ranking in enumerate(rankings):
        weight = 1.0 if weights is None else float(weights[index])
        if weight <= 0:
            continue
        for position, pid in enumerate(ranking):
            scores[pid] += weight / (k + position + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def fts_ranking(con: sqlite3.Connection, terms: list[str], *, limit: int,
                venue: str | None = None, year: int | None = None) -> list[str]:
    """FTS ranking: all the query's terms first, then as many as will fit.

    WHY NOT JUST AN OR
    ------------------
    The old expression ORed the terms, so "graph neural network" matched every
    paper containing the word "network" -- 47,572 of them, of which most are
    about something else entirely. The prefix expansion still has to be there
    ("knowledge graph" vs "knowledge graphs"), but the terms themselves
    should be required together:

        ("graph" OR graph*) AND ("neural" OR neural*) AND ("network" OR network*)
        -> 2,509 papers

    WHY NOT JUST AN AND
    -------------------
    A long title ORed is unusable, and ANDed it can be too strict: 24 title
    tokens usually leave one paper. So the terms are ordered by how many
    papers they match (rarest first), the AND is attempted with all of them,
    and the least selective terms are dropped one at a time until enough
    papers come back. Dropping the most common term first is what a search
    engine does when a query is too narrow.

    The old code also capped the expression at 8 terms, silently dropping the
    rest: 93 of the 122 title queries that failed to find their own paper
    had more than 8 tokens (median 11, max 28). Nothing is dropped now
    except function words, which match nearly everything anyway.
    """
    clean = _clean_terms(terms)
    if not clean:
        return []
    content = _content_terms(clean)

    # Rarest term first, so the AND starts from the most selective filter.
    ordered = sorted(content, key=lambda t: _selectivity(con, t))
    wanted = max(limit, 30)

    # Try the AND with everything, then relax by dropping the most common
    # terms. Each relaxation is one query; the cap keeps a long title from
    # turning into a dozen queries.
    attempts = 0
    for keep in range(len(ordered), 1, -1):
        subset = ordered[:keep]
        rows = _fts_query(con, _and_expression(subset), limit=wanted,
                          venue=venue, year=year)
        if len(rows) >= wanted or attempts >= 3:
            return rows[:limit]
        attempts += 1

    # Last resort: any of the terms, so a query that matches nothing together
    # still matches something.
    rows = _fts_query(con, _or_expression(ordered), limit=wanted,
                      venue=venue, year=year)
    if rows:
        return rows[:limit]
    return _fts_query(con, _or_expression(clean), limit=wanted,
                      venue=venue, year=year)[:limit]


def lexical_ranking(con: sqlite3.Connection, raw_query: str, *, limit: int,
                    venue: str | None = None, year: int | None = None) -> list[str]:
    """Rank papers by lexical evidence alone, in the order they should be shown.

    This is the Papers page's search box, where the user is looking for a
    paper rather than exploring a topic. It fuses the same lexical channels
    the semantic search uses -- exact title phrase, all-terms, any-term,
    substring -- and nothing else: a paper list has no vector to rank by
    without loading a model.

    The channels are weighted by how much they prove: a title that contains
    the whole query is the strongest possible evidence, the all-terms channel
    is strong, the any-terms channel is weak, and the substring channel is a
    guess.
    """
    terms = [t for t in re.findall(r"[\w一-鿿]+", str(raw_query)) if len(t) >= 2]
    if not terms:
        return []
    phrase = phrase_ranking(con, raw_query, limit=limit, venue=venue, year=year)
    fts = fts_ranking(con, terms, limit=limit, venue=venue, year=year)
    like = like_ranking(con, raw_query, limit=limit, venue=venue, year=year)
    fused = rank_fuse([phrase, fts, like], weights=[3.0, 1.2, 0.6])
    return [pid for pid, _score in fused[:limit]]


def like_ranking(con: sqlite3.Connection, raw_query: str, *, limit: int,
                venue: str | None = None, year: int | None = None) -> list[str]:
    """Substring ranking, for the one case FTS cannot serve.

    This channel exists so that ``SECI`` finds "Secilia-Cxy" and "baseCIL":
    FTS tokenises on punctuation and case, so those never become the token
    "seci", while a LIKE scan sees the four letters inside the larger word.

    WHY IT IS NOW GATED ON SHORT QUERIES
    ------------------------------------
    A LIKE with a leading wildcard cannot use an index, so each term is a full
    scan of ``papers`` -- and it scanned both ``title`` and ``abstract``,
    which is where the cost lives. On the 288k corpus that is 2.4-5.5 s per
    query, and it was the dominant cost of whole-corpus search (the cosine
    product is 6 ms).

    It was also, for an ordinary query, contributing nothing. Measured on the
    real corpus, comparing this channel against the FTS channel:

        query                                fts  like  overlap  like-only
        'single-cell RNA sequencing'          60    30        0         30
        'large language model reasoning'      60    30        0         30
        'transformers for protein structure'  60    30        0         30
        'graph neural network drug discovery' 60    30        0         30
        'diffusion'                           60    30        0         30
        'SECI'                                13    12       11          1
        'baseCIL'                              1     1        1          0

    For a multi-word query the two rankings do not even overlap -- LIKE
    ordered by title returns 30 papers the FTS ranking never chose -- so the
    scan was spending seconds to inject noise into the RRF fusion. The value
    appears only for a short query, where an acronym is the whole question.

    So the channel runs only when the query is a single short token (a
    likely acronym or identifier). That is the case it was built for, and it
    is the only case where the scan earns its cost.
    """
    terms = [t for t in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}", raw_query)][:3]
    if not terms or len(terms) > 1:
        # A multi-word query is what FTS is for; see the measurements above.
        return []
    term = terms[0]
    if len(term) > 24:
        # Long enough to be a phrase, not an identifier worth scanning for.
        return []
    # Both columns: the occurrences this channel exists to catch are not
    # always in the title -- SECI's "Secilia-Cxy" is a username in the
    # abstract, and "baseCIL" is a repository name there. Scanning titles
    # alone would drop two of the three hits the channel was built for.
    like = "lower(p.title) LIKE ? OR lower(p.abstract) LIKE ?"
    sql_args: list[Any] = [f"%{term.lower()}%", f"%{term.lower()}%"]
    where_extra = []
    if venue is not None:
        where_extra.append("EXISTS (SELECT 1 FROM paper_venues pv WHERE pv.paper_id=p.id AND pv.venue_id=?)")
        sql_args.append(venue)
    if year is not None:
        where_extra.append("EXISTS (SELECT 1 FROM paper_venues pv2 WHERE pv2.paper_id=p.id AND pv2.year=?)")
        sql_args.append(year)
    clause = f"({like})" + ("".join(f" AND {w}" for w in where_extra))
    sql = f"SELECT p.id FROM papers p WHERE {clause} ORDER BY p.title LIMIT ?"
    sql_args.append(max(limit, 30))
    return [row["id"] for row in con.execute(sql, sql_args)]


def suggest_terms(query: str, vocabulary: Iterable[str], *, limit: int = 6) -> list[str]:
    """Vocabulary terms close to the query's spelling ("did you mean").

    ``difflib.get_close_matches`` compares by longest matching subsequences,
    which behaves like a forgiving edit distance for short acronyms: ``SECI``
    yields ``semi``/``social``/``series`` from the model's vocabulary. Uppercase
    acronyms keep their case in the suggestion.
    """
    tokens = [t for t in re.findall(r"[A-Za-z][A-Za-z0-9-]+", query) if len(t) >= 3][:4]
    out: list[str] = []
    seen: set[str] = set()
    vocab = list(vocabulary)
    for token in tokens:
        for match in difflib.get_close_matches(token.lower(), vocab, n=limit, cutoff=0.6):
            if match not in seen:
                seen.add(match)
                out.append(match)
        if len(out) >= limit:
            break
    return out[:limit]


def pseudo_relevance_feedback(terms: list[str], top_ids: list[str],
                              fetch: Callable[[list[str]], list[tuple[str, str]]],
                              *, limit: int = 6) -> list[str]:
    """Expand ``terms`` with frequent words from the top-ranked documents (PRF).

    Classic blind feedback: assume the first few hits are relevant, take their
    most common non-query words, and let the *caller* decide what to do with
    them (here: offered as one-click refinements, not silently rewritten).
    """
    if not top_ids:
        return []
    counts: dict[str, int] = defaultdict(int)
    already = {t.lower() for t in terms}
    for _pid, text in fetch(top_ids[:5]):
        for word in re.findall(r"[a-z][a-z-]{3,}", (text or "").lower()):
            if word in already or word in _STOP:
                continue
            counts[word] += 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]


#: Cheap English stopword set for PRF; the vectorizer's own list is not exposed.
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "have", "has", "had", "not", "but", "can", "may", "our", "their", "its",
    "into", "such", "than", "then", "them", "these", "those", "there", "which",
    "will", "would", "could", "should", "also", "more", "most", "other", "using",
    "based", "paper", "propose", "propose", "method", "methods", "approach",
    "results", "show", "shown", "table", "figure", "model", "models", "task",
    "tasks", "data", "dataset", "datasets", "training", "performance", "propose",
    "between", "different", "however", "where", "when", "while", "each", "both",
}


def fetch_titles(con: sqlite3.Connection, ids: list[str]) -> list[tuple[str, str]]:
    rows = []
    for pid in ids:
        row = con.execute("SELECT title FROM papers WHERE id=?", (pid,)).fetchone()
        if row is not None:
            rows.append((pid, row["title"]))
    return rows


def fetch_titles_and_abstracts(con: sqlite3.Connection, ids: list[str]) -> list[tuple[str, str]]:
    rows = []
    for pid in ids:
        row = con.execute("SELECT title, abstract FROM papers WHERE id=?", (pid,)).fetchone()
        if row is not None:
            rows.append((pid, (row["title"] or "") + " " + (row["abstract"] or "")))
    return rows
