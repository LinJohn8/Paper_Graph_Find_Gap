"""Europe PMC as a journal collector.

WHY THIS SOURCE EXISTS
----------------------
The journal corpus was built from OpenAlex, which indexes 122,936 articles
across fifteen titles -- and which meters requests per IP against a small
shared daily budget when no API key is supplied. The budget ran out twice
during this work, and the collection stalled on a `429` that no amount of
retrying can fix.

Europe PMC is the alternative that does not have that failure mode:

* it is a public, legitimate metadata service (the EBI's), needs no key and
  no registration;
* it is *not* paywalled: it serves the abstract of every record it holds;
* it pages with a cursor, so there is no 10,000-record ceiling (OpenAlex
  stops at 10,000 works per year without a key, which is why Nature
  Communications 2024-2026 sit at exactly 10,000);
* it indexes biomedical journals the OpenAlex registry never listed at all.

Measured on 2026-10-21, one 2024 query each:

    journal            records    journal            records
    PLOS ONE            17,774    Nature               4,100
    Nature Comm.        10,931    BMJ                  2,724
    eLife                1,632    JAMA                 1,570
    Cell Reports         1,441    IEEE TPAMI             753
    Nature Medicine        734    Science Robotics        137
    Nature Neuroscience    310

Two of those -- IEEE Transactions on Pattern Analysis and Machine
Intelligence and Nature Machine Intelligence -- are the machine-learning
venues the corpus was weakest on, and Europe PMC serves their abstracts in
full while OpenAlex often withholds them.

The records are *journal articles* only: the query filters on
``FIRST_PDATE`` (first publication date) inside the year and on the journal
title, and ``SRC:MED`` is not used, so preprints and non-indexed material
stay out of a journal's count.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterator

import httpx

from . import crawl
from . import normalize

LOGGER = logging.getLogger("paper_ingest.provider_epmc")

EUROPEPMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

#: Europe PMC's own page size. 1000 is the documented maximum for the JSON
#: result type and keeps the response body manageable.
PAGE_SIZE = 1000

#: The journals Europe PMC serves, keyed by the registry key.
#:
#: The value is a *family*: the tuple of MEDLINE titles Europe PMC indexes
#: under, and the display name the corpus shows. A family exists because
#: OpenAlex treats a publisher's titles as one source while Europe PMC
#: indexes each MEDLINE title separately -- and the wildcard
#: ``JOURNAL:"Lancet*"`` is not supported (measured: 0 hits for every family
#: tried), so the titles have to be listed.
#:
#: Every title below was measured against the live service on 2026-10-21 with
#: a 2024 query; the counts are the records *with an abstract*, which is what
#: a journal-year backfill actually needs. The "family" column is what the
#: whole family yields against the flagship alone:
#:
#:     family      flagship      family      difference
#:     Nature        1,280      15,297          12.0x
#:     Cell            533       3,843           7.2x
#:     Lancet          236       1,505           6.4x
#:     JAMA            202       3,314          16.4x
#:     BMJ             132      10,615          80.4x
#:
#: That difference is not decoration: without the specials, The Lancet's
#: abstract coverage was 6% of its papers, because the corpus's 1,373 Lancet
#: papers include 1,269 that Europe PMC files under the specials.
#:
#: Only titles that carry research articles are listed. Reviews
#: (``Nat Rev *``) and case reports (``BMJ Case Rep``) are excluded on
#: purpose: they are a different kind of document from the research paper
#: this corpus is about.
EPMC_JOURNALS: dict[str, tuple[tuple[str, ...], str]] = {
    # -- the journals the OpenAlex registry already covers -----------------
    "nature": (
        ("Nature", "Nat Commun", "Nat Methods", "Nat Med", "Nat Neurosci",
         "Nat Biotechnol", "Nat Genet", "Nat Chem", "Nat Phys", "Nat Mater",
         "Nat Nanotechnol", "Nat Mach Intell", "Nat Comput Sci", "Nat Sustain",
         "Nat Food", "Nat Cell Biol", "Nat Struct Mol Biol", "Nat Aging",
         "Nat Hum Behav", "Nat Metab", "Nat Biomed Eng", "Nat Protoc",
         "Nat Commun Biol"),
        "Nature"),
    "science": (
        ("Science", "Sci Adv", "Sci Robot", "Sci Signal", "Sci Transl Med",
         "Sci Data"),
        "Science"),
    "cell": (
        ("Cell", "Cell Rep", "Cell Rep Med", "Cell Rep Methods", "Cell Stem Cell",
         "Cell Metab", "Cell Host Microbe", "Cell Syst", "Cell Genom",
         "Cell Chem Biol", "Cell Death Differ", "Cell Mech Biol",
         "Cell Mol Biol", "Cell Tissue Res", "Cell Cycle", "Cell Transplant"),
        "Cell"),
    "neuron": (("Neuron", "Neuron (Cambridge)"), "Neuron"),
    "prl": (("Phys Rev Lett", "Phys Rev Lett (Online)"), "Physical Review Letters"),
    "jacs": (("J Am Chem Soc", "J Am Chem Soc (Online)"),
             "Journal of the American Chemical Society"),
    "lancet": (
        ("Lancet", "Lancet Oncol", "Lancet Infect Dis", "Lancet Microbe",
         "Lancet Public Health", "Lancet Digit Health", "Lancet Rheumatol",
         "Lancet Neurol", "Lancet Gastroenterol Hepatol", "Lancet Healthy Longev",
         "Lancet Haematol", "Lancet Psychiatry", "Lancet Diabetes Endocrinol",
         "Lancet Respir Med", "Lancet Child Adolesc Health", "Lancet Reg Health"),
        "The Lancet"),
    "naturecomms": (("Nat Commun",), "Nature Communications"),
    "naturemi": (("Nat Mach Intell",), "Nature Machine Intelligence"),
    "natureneuro": (("Nat Neurosci",), "Nature Neuroscience"),
    "naturemethods": (("Nat Methods",), "Nature Methods"),
    "npjqi": (("npj Quantum Inf",), "npj Quantum Information"),
    "tpami": (("IEEE Trans Pattern Anal Mach Intell",
               "IEEE Trans Pattern Anal Mach Intell (Online)"),
              "IEEE Transactions on Pattern Analysis and Machine Intelligence"),
    "ijcv": (("Int J Comput Vis", "Int J Comput Vis (Online)"),
             "International Journal of Computer Vision"),
    # -- journals the corpus did not have at all ---------------------------
    "bmj": (
        ("BMJ", "BMJ Open", "BMJ Glob Health", "BMJ Public Health",
         "BMJ Open Diabetes Res Care", "BMJ Open Sport Exerc Med",
         "BMJ Open Resp Res", "BMJ Paediatr Open", "BMJ Open Res"),
        "BMJ"),
    "jama": (
        ("JAMA", "JAMA Netw Open", "JAMA Intern Med", "JAMA Cardiol",
         "JAMA Oncol", "JAMA Psychiatry", "JAMA Pediatr", "JAMA Dermatol",
         "JAMA Ophthalmol", "JAMA Neurol", "JAMA Surg", "JAMA Health Forum"),
        "JAMA"),
    "plomed": (("PLoS Med",), "PLOS Medicine"),
    "plosone": (("PLoS One",), "PLOS ONE"),
    "elife": (("Elife",), "eLife"),
    "cellrep": (("Cell Rep",), "Cell Reports"),
    "natmed": (("Nat Med",), "Nature Medicine"),
    "scirobot": (("Sci Robot",), "Science Robotics"),
    "nejm": (("N Engl J Med", "N Engl J Med (Online)"),
             "New England Journal of Medicine"),
    "pnas": (("Proc Natl Acad Sci U S A", "Proc Natl Acad Sci U S A (Online)"),
             "Proceedings of the National Academy of Sciences"),
    "ieee_access": (("IEEE Access", "IEEE Access (Online)"), "IEEE Access"),
}

#: The journals Europe PMC covers that the OpenAlex registry never listed.
#: These are the ones the Europe PMC collector can add to the corpus on its
#: own -- measured 2024, records with an abstract:
#:
#:     plosone     16,617     elife     1,539     cellrep   1,374
#:     bmj            132     natmed      328     plomed      145
#:     jama           202     scirobot    132     nejm        228
#:     pnas         3,372
EPMC_ONLY_JOURNALS: tuple[str, ...] = (
    "bmj", "jama", "plomed", "plosone", "elife", "cellrep",
    "natmed", "scirobot", "nejm", "pnas",
)

__all__ = [
    "EPMC_JOURNALS",
    "EPMC_ONLY_JOURNALS",
    "EUROPEPMC_API",
    "EpmcProvider",
    "epmc_query",
]


def epmc_query(journal: str | tuple[str, ...], year: int,
               *, since: str | None = None) -> str:
    """The Europe PMC query for one journal-year.

    ``FIRST_PDATE`` is the journal's own first-publication date, which is the
    right window for a journal: a paper published online in December 2023 and
    in print in January 2024 belongs to the 2024 issue. ``PUB_TYPE:"journal
    article"`` keeps reviews, editorials and letters out of the count --
    without it, a year is inflated by material that is not a research paper.

    ``journal`` may be a family (several MEDLINE titles) as well as a single
    one. The family matters: OpenAlex treats a publisher's family as one
    source -- "The Lancet" covers the flagship *and* its specials -- while
    Europe PMC indexes every MEDLINE title separately, and the wildcard
    ``JOURNAL:"Lancet*"`` is not supported (measured: 0 hits for every
    family tried). So the titles have to be listed and OR-ed together.
    Measured for 2024, records with an abstract:

        family      flagship only      whole family
        Lancet             236             1,505
        Cell               533             3,843
        Nature           1,280            15,297
        JAMA               202             3,314
        BMJ                132            10,615
        PLoS One        16,617            37,348

    ``since`` narrows the query to the records Europe PMC *created* after
    that date (``CREATION_DATE``), which is the same idea as OpenAlex's
    ``from_created_date``: a settled journal-year only changes as the index
    catches up, so an incremental run should ask for what is new rather than
    for the whole year again. The date is ``YYYY-MM-DD``; a full timestamp is
    truncated to its date part.
    """
    titles = (journal,) if isinstance(journal, str) else tuple(journal)
    if len(titles) == 1:
        journal_clause = f'JOURNAL:"{titles[0]}"'
    else:
        journal_clause = "(" + " OR ".join(f'JOURNAL:"{t}"' for t in titles) + ")"
    query = (
        f'{journal_clause} AND FIRST_PDATE:[{year}-01-01 TO {year}-12-31] '
        f'AND (PUB_TYPE:"journal article" OR PUB_TYPE:"research article")'
    )
    since_date = (since or "").strip()[:10]
    if since_date:
        query += f' AND CREATION_DATE:[{since_date} TO 2999-12-31]'
    return query


def _clean_abstract(value: Any) -> str | None:
    """Europe PMC abstracts arrive as plain text, sometimes with markup.

    The service returns the abstract with ``<h4>`` section labels
    ("BACKGROUND", "METHODS", ...) in some records. They are part of the
    text, not markup, so they are kept -- but the entity and tag noise
    around them is removed.
    """
    if not isinstance(value, str):
        return None
    text = value.replace("<h4>", " ").replace("</h4>", " ")
    text = re.sub(r"<[^>]+>", " ", text)
    text = normalize.clean_text(text)
    return text or None


class EpmcProvider:
    """Harvest one journal-year from Europe PMC.

    The provider is deliberately narrow: one journal, one year, and a
    complete abstract for every record it serves. It is the fallback that
    works when the OpenAlex budget is spent, and the source for the titles
    OpenAlex never indexed.
    """

    def __init__(
        self,
        venue_key: str,
        *,
        mailto: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        sleep: float = 0.2,
    ) -> None:
        if venue_key not in EPMC_JOURNALS:
            raise ValueError(
                f"unknown journal {venue_key!r}; known: {', '.join(sorted(EPMC_JOURNALS))}")
        self.venue_key = venue_key
        # The registry value is (family of MEDLINE titles, display name).
        self.journals, self.journal_name = EPMC_JOURNALS[venue_key]
        #: The flagship title, for the source id and for messages.
        self.journal = self.journals[0]
        self.sleep = max(0.0, float(sleep))
        self.mailto = (mailto or "").strip() or "research@example.org"
        self.headers = {
            "User-Agent": crawl.polite_user_agent("PaperGraph", "0.2", self.mailto),
            "Accept": "application/json",
        }
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout, follow_redirects=True, headers=self.headers)
        self._policy = crawl.RetryPolicy(attempts=5, base_delay=1.0, max_delay=30.0,
                                         jitter=0.25)
        #: The number of records the last query reported, per the service.
        self.last_hit_count: int | None = None
        #: How many pages the last run actually fetched.
        self.last_pages = 0

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "EpmcProvider":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- transport ---------------------------------------------------------
    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        """One request, with the shared retry policy."""
        response = crawl.fetch_with_policy(
            self._client, EUROPEPMC_API, params=params, policy=self._policy)
        return response.json()

    # -- harvesting --------------------------------------------------------
    def _query(self, year: int, *, has_abstract_only: bool = True,
               since: str | None = None) -> str:
        """The query for one journal-year, with the abstract filter applied.

        The whole family is queried, not just the flagship: Europe PMC
        indexes each MEDLINE title separately, so asking for one title
        misses the specials -- which is where the majority of The Lancet's
        papers live.
        """
        query = epmc_query(self.journals, year, since=since)
        if has_abstract_only:
            query += " AND HAS_ABSTRACT:Y"
        return query

    def iter_year(self, year: int, *, limit: int | None = None,
                  has_abstract_only: bool = True,
                  since: str | None = None) -> Iterator[dict[str, Any]]:
        """Yield the normalised records of one journal-year.

        Paging uses Europe PMC's ``cursorMark``: it is the service's own
        cursor and has no record ceiling. The loop stops when a page returns
        fewer records than the page size, when the cursor stops moving, or
        when the limit is reached -- any of the three means "no more".

        ``has_abstract_only`` adds the service's ``HAS_ABSTRACT:Y`` filter.
        Measured on 2024: BMJ 1,178 -> 132, JAMA 947 -> 202, PLOS ONE
        16,688 -> 16,617, Cell Reports 1,376 -> 1,374, IEEE TPAMI 753 -> 753.
        The filter costs nothing where the abstracts are there, and it
        removes a large tail of editorial matter where they are not.

        ``since`` asks only for the records Europe PMC created after that
        date: the incremental form, the same idea as OpenAlex's
        ``from_created_date``.
        """
        params: dict[str, Any] = {
            "query": self._query(year, has_abstract_only=has_abstract_only,
                                 since=since),
            "format": "json",
            "resultType": "core",
            "pageSize": PAGE_SIZE,
            "cursorMark": "*",
        }
        cursor = "*"
        seen = 0
        self.last_pages = 0
        while True:
            params["cursorMark"] = cursor
            payload = self._get(params)
            results = payload.get("resultList", {}).get("result", []) or []
            if self.last_hit_count is None:
                try:
                    self.last_hit_count = int(payload.get("hitCount"))
                except (TypeError, ValueError):
                    self.last_hit_count = None
            self.last_pages += 1
            if not results:
                break
            for record in results:
                paper = self.normalize(record, year)
                if paper is None:
                    continue
                yield paper
                seen += 1
                if limit is not None and seen >= limit:
                    return
            if len(results) < PAGE_SIZE:
                break
            next_cursor = payload.get("nextCursorMark")
            # The service answers the last page with the same cursor (or with
            # none). A cursor that does not move would loop forever, so that is
            # the end of the collection.
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            if self.sleep:
                time.sleep(self.sleep)

    def list_year(self, year: int, *, limit: int | None = None,
                  has_abstract_only: bool = True,
                  since: str | None = None) -> list[dict[str, Any]]:
        """All the records of one journal-year."""
        return list(self.iter_year(year, limit=limit,
                                   has_abstract_only=has_abstract_only,
                                   since=since))

    # -- normalisation -----------------------------------------------------
    def normalize(self, record: dict[str, Any], year: int) -> dict[str, Any] | None:
        """Map one Europe PMC record onto the paper-dict contract.

        Returns ``None`` for a record without a usable title: ``papers.title``
        is NOT NULL and anchors the FTS index, so such a record cannot be
        stored.
        """
        title = normalize.clean_text(record.get("title"))
        if not title:
            return None
        pmid = str(record.get("pmid") or "").strip()
        doi = normalize.normalize_doi(record.get("doi"))
        source_id = str(record.get("id") or "").strip()
        # Europe PMC's own id is "MED/<pmid>" or "PMC<id>"; the pmid is the
        # stable identifier for a journal article.
        paper_id = f"epmc:{pmid}" if pmid else f"epmc:{source_id or doi or title[:40]}"
        if not pmid and not doi and not source_id:
            return None

        authors = self._authors(record)
        keywords = self._keywords(record)
        abstract = _clean_abstract(record.get("abstractText"))
        first_date = str(record.get("firstPublicationDate") or "")
        publication_year = self._year(first_date) or year

        return {
            "id": paper_id,
            "source": "europepmc",
            "source_paper_id": pmid or source_id,
            "forum_id": None,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "keywords": keywords,
            "primary_area": record.get("journalTitle") or self.journal_name,
            "pdf_url": None,
            "paper_url": f"https://europepmc.org/article/{source_id}" if source_id else None,
            "doi": doi,
            "arxiv_id": None,
            "submission_year": year,
            "publication_year": publication_year,
            "status": "published",
            "decision": None,
            "created_at_source": first_date or None,
            "updated_at_source": str(record.get("dateOfCreation") or "") or None,
            "raw_data_path": None,
        }

    @staticmethod
    def _authors(record: dict[str, Any]) -> list[str]:
        """The author list, in the order the record gives it.

        Europe PMC offers two spellings: ``authorString`` ("Smith J, Doe A")
        and ``authorList`` (structured, with the given and family names
        separated). The structured form is preferred because it can be
        normalised into one spelling per person; the flat string is the
        fallback.
        """
        names: list[str] = []
        author_list = record.get("authorList")
        if isinstance(author_list, dict):
            author_list = author_list.get("author")
        if isinstance(author_list, list):
            for entry in author_list:
                if not isinstance(entry, dict):
                    continue
                full = entry.get("fullName")
                if isinstance(full, str) and full.strip():
                    names.append(normalize.normalize_author_name(full))
                    continue
                last = entry.get("lastName")
                first = entry.get("firstName") or entry.get("initials")
                if isinstance(last, str) and last.strip():
                    names.append(normalize.normalize_author_name(
                        f"{first or ''} {last}".strip()))
        if names:
            return names
        flat = record.get("authorString")
        if isinstance(flat, str) and flat.strip():
            for part in flat.split(","):
                cleaned = normalize.normalize_author_name(part)
                if cleaned:
                    names.append(cleaned)
        return names

    @staticmethod
    def _keywords(record: dict[str, Any]) -> list[str]:
        """The record's keyword list, de-duplicated."""
        raw = record.get("keywordList")
        if isinstance(raw, dict):
            raw = raw.get("keyword")
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for entry in raw:
            if not isinstance(entry, str):
                continue
            label = normalize.clean_text(entry)
            if not label or label.lower() in seen:
                continue
            seen.add(label.lower())
            out.append(label)
        return out

    @staticmethod
    def _year(value: str) -> int | None:
        """The year of a ``YYYY-MM-DD`` (or ``YYYY``) date."""
        match = re.match(r"(\d{4})", value or "")
        return int(match.group(1)) if match else None
