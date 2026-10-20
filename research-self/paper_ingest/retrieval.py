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


def rank_fuse(rankings: list[list[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Fuse id rankings by Reciprocal Rank Fusion.

    Each entry in ``rankings`` is a list of paper ids, best first. Returns
    ``(id, fused_score)`` best first. RRF needs no score normalisation between
    channels, which is exactly what a cosine-vs-BM25 fusion needs.
    """
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for position, pid in enumerate(ranking):
            scores[pid] += 1.0 / (k + position + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def fts_ranking(con: sqlite3.Connection, terms: list[str], *, limit: int,
                venue: str | None = None, year: int | None = None) -> list[str]:
    """FTS ranking: whole tokens plus prefix tokens, joined by OR.

    A bare ``MATCH 'seci'`` only hits papers containing that exact token. Real
    queries are prefixes of what the author wrote ("knowledge graph" vs
    "knowledge graphs"), so every term is also tried as ``term*``. OR keeps
    recall high. bm25() is only usable with the FTS table as the plain outer
    query — it raises "unable to use function bm25 in the requested context"
    under a JOIN/GROUP BY — so ranking happens on the FTS side and venue/year
    filtering on the papers side. Terms are sanitised (quotes stripped) because
    they are interpolated into the MATCH expression, which SQLite bindings do
    not parameterise.
    """
    if not terms:
        return []
    # Keep only safe characters: the expression is interpolated (FTS MATCH is
    # not parameterisable), and quotes around a prefix actually *disable* it
    # ('"learn"' means the exact phrase, '"learn"*' matches nothing).
    clean = [t for t in (re.sub(r"[^\w\u4e00-\u9fff]", "", term) for term in terms) if len(t) >= 2]
    if not clean:
        return []
    # Per term: exact token OR prefix expansion, terms joined by OR as well.
    # The OR is required — '"learn" learn*' would be an implicit AND.
    match_expr = " OR ".join(f'"{t}" OR {t}*' for t in clean[:8])
    try:
        ranked = [
            row["paper_id"]
            for row in con.execute(
                "SELECT paper_id FROM papers_fts "
                f"WHERE papers_fts MATCH '{match_expr}' "
                "ORDER BY bm25(papers_fts) LIMIT ?",
                (max(limit * 2, 60),),
            )
        ]
    except sqlite3.OperationalError:
        # An FTS syntax error must not kill the whole search.
        return []
    if not ranked:
        return []
    # Scope filter on the papers side (bm25 order preserved).
    args: list[Any] = []
    where_extra = []
    if venue is not None:
        where_extra.append("EXISTS (SELECT 1 FROM paper_venues pv WHERE pv.paper_id=p.id AND pv.venue_id=?)")
        args.append(venue)
    if year is not None:
        where_extra.append("EXISTS (SELECT 1 FROM paper_venues pv2 WHERE pv2.paper_id=p.id AND pv2.year=?)")
        args.append(year)
    clause = " AND ".join(where_extra)
    sql = f"SELECT p.id FROM papers p WHERE p.id IN ({','.join('?' * len(ranked))})"
    if clause:
        sql += f" AND {clause}"
    sql += " LIMIT ?"
    kept = {row["id"] for row in con.execute(sql, (*ranked, *args, limit))}
    return [pid for pid in ranked if pid in kept][:limit]


def like_ranking(con: sqlite3.Connection, raw_query: str, *, limit: int,
                venue: str | None = None, year: int | None = None) -> list[str]:
    """Substring ranking over titles AND abstracts.

    This is the channel that finds "Secilia-Cxy" and "baseCIL" for ``SECI``: FTS
    tokenises on punctuation and case, so those never become the token "seci",
    but a LIKE scan sees the four letters inside the larger word. It is the
    slowest channel (a scan per term), so it is capped at three terms and a
    small result set — it contributes ordering to the RRF fusion, not volume.
    """
    terms = [t for t in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}", raw_query)][:3]
    if not terms:
        return []
    like = " OR ".join(["lower(p.title) LIKE ? OR lower(p.abstract) LIKE ?"] * len(terms))
    args: list[Any] = []
    for t in terms:
        args.extend([f"%{t.lower()}%", f"%{t.lower()}%"])
    where_extra = []
    if venue is not None:
        where_extra.append("EXISTS (SELECT 1 FROM paper_venues pv WHERE pv.paper_id=p.id AND pv.venue_id=?)")
        args.append(venue)
    if year is not None:
        where_extra.append("EXISTS (SELECT 1 FROM paper_venues pv2 WHERE pv2.paper_id=p.id AND pv2.year=?)")
        args.append(year)
    clause = f"({like})" + ("".join(f" AND {w}" for w in where_extra))
    sql = f"SELECT p.id FROM papers p WHERE {clause} ORDER BY p.title LIMIT ?"
    args.append(max(limit, 30))
    return [row["id"] for row in con.execute(sql, args)]


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
