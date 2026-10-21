"""Field normalisation shared by every collector.

WHY THIS MODULE EXISTS
-----------------------
The corpus is assembled from sources that spell the same field differently.
OpenAlex writes an author as ``"Joshua R. Wang"``; Crossref writes
``given``/``family`` pairs that the provider joins as ``"Joshua R. Wang"``;
JMLR's volume page prints ``"Benjamin Moseley, Joshua R. Wang"``; a scraped
CVF page can print ``"Wang, Joshua R."`` or ``"J. R. Wang"`` or
``"J.R. Wang"``. The DOI column is worse: ``https://doi.org/10.1038/x``,
``doi:10.1038/x`` and ``10.1038/x`` are the same identifier in three shapes.

Two concrete failures came out of that:

* **Author search missed.** A query for an author matched one source's spelling
  and not another's, so the same researcher appeared under two identities.
* **Deduplication missed.** ``harvest.paper_keys`` falls back to a normalised
  title, but a preprint and its published version can differ in more than
  punctuation, and neither key matched.

The functions here reduce a field to a *canonical display form* and to a
*comparison key*. The display form is what the corpus stores and shows; the
key is what search and dedup compare. Keys are deliberately lossy (they drop
the given name, keep the family name and the first initial) because that is
what survives the spelling differences above.

Everything here is a pure function: no network, no database, no clock. That
is deliberate -- these are the rules every collector must agree on, so they
have to be testable on their own.
"""
from __future__ import annotations

import re
import unicodedata

__all__ = [
    "normalize_whitespace",
    "normalize_doi",
    "normalize_author_name",
    "author_key",
    "author_keys",
    "normalize_title",
    "normalize_venue_name",
    "clean_text",
]

_WS_RE = re.compile(r"\s+")
_DOI_PREFIX_RE = re.compile(
    r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*|info:doi/)", re.IGNORECASE
)
_PUNCT_RE = re.compile(r"[^a-z0-9]+")
#: LaTeX leftovers that survive in scraped titles and abstracts.
_LATEX_CMD_RE = re.compile(r"\\([a-zA-Z]+)")
_LATEX_JUNK_RE = re.compile(r"[{}\$]")

#: LaTeX commands that only *format* their argument. The command name carries
#: no meaning of its own, so it is dropped: ``\emph{deep}`` -> ``deep``.
#: Symbolic commands are different -- ``\log`` and ``\sum`` are words the
#: reader needs -- and their names are kept.
_LATEX_FORMATTING = frozenset({
    "emph", "textbf", "textit", "texttt", "textsf", "textrm", "textsc",
    "mathrm", "mathbf", "mathit", "mathcal", "mathbb", "mathfrak", "mathsf",
    "text", "underline", "overline", "mbox", "hbox", "begin", "end", "item",
    "caption", "label", "ref", "cite", "citep", "citet", "footnote",
    "section", "subsection", "subsubsection", "paragraph", "title", "author",
    "keywords", "url", "href", "includegraphics", "bibliography", "bibitem",
    "noindent", "centering", "newblock", "newline", "linebreak", "small",
    "large", "Large", "LARGE", "tiny", "scriptsize", "footnotesize",
    "textsc", "ensuremath", "mathop", "mathbin", "stackrel", "widetilde",
})

#: Name particles that must stay attached to the family name.
_PARTICLES = frozenset({
    "van", "von", "de", "del", "della", "der", "di", "da", "dos", "das",
    "du", "la", "le", "el", "al", "bin", "ibn", "ter", "ten", "af", "av",
    "st", "st.", "mac", "mc", "san", "santa", "abu",
})

#: Venue abbreviations seen in the corpus, mapped to the full title. These are
#: not derivable: "J. Mach. Learn. Res." and "Journal of Machine Learning
#: Research" share no letters to key on, so the mapping has to be stated.
VENUE_ALIASES: dict[str, str] = {
    "jmlr": "Journal of Machine Learning Research",
    "tpami": "IEEE Transactions on Pattern Analysis and Machine Intelligence",
    "ijcv": "International Journal of Computer Vision",
    "prl": "Physical Review Letters",
    "jacs": "Journal of the American Chemical Society",
    "tmlr": "Transactions on Machine Learning Research",
    "nmi": "Nature Machine Intelligence",
    "ncomms": "Nature Communications",
    "cvpr": "IEEE/CVF Conference on Computer Vision and Pattern Recognition",
    "iccv": "IEEE/CVF International Conference on Computer Vision",
    "eccv": "European Conference on Computer Vision",
    "neurips": "Advances in Neural Information Processing Systems",
    "nips": "Advances in Neural Information Processing Systems",
}


def normalize_whitespace(value: str | None) -> str:
    """Collapse runs of whitespace and trim. Empty input -> empty string."""
    if not value:
        return ""
    return _WS_RE.sub(" ", str(value)).strip()


def clean_text(value: str | None) -> str:
    """Strip LaTeX markup and collapse whitespace.

    Scraped proceedings pages carry ``$O(n \\log n)$`` and ``\\emph{...}``
    fragments that pollute the FTS index and the cluster labels. The two kinds
    of command are treated differently: a *formatting* command (``\\emph``,
    ``\\textbf``) only decorates its argument and is dropped, while a
    *symbolic* one (``\\log``, ``\\sum``) is a word the reader needs and is
    kept. Braces, dollars and stray backslashes go either way.
    """
    if not value:
        return ""

    def _replace(match: re.Match[str]) -> str:
        command = match.group(1)
        if command in _LATEX_FORMATTING:
            return " "
        return f" {command} "

    text = _LATEX_CMD_RE.sub(_replace, str(value))
    text = _LATEX_JUNK_RE.sub(" ", text)
    text = text.replace("\\&", "&").replace("~", " ")
    return normalize_whitespace(text)


def normalize_doi(value: str | None) -> str | None:
    """The bare DOI (``10.x/y``), or ``None``.

    Sources disagree on shape -- ``https://doi.org/10.x/y`` (OpenAlex),
    ``doi:10.x/y`` (scrape metadata), ``10.x/y`` (Crossref). The column holds
    one identifier, so it is reduced to the bare form here. Case is preserved:
    DOIs are case-insensitive, and the dedup keys lowercase separately.
    """
    if not value or not isinstance(value, str):
        return None
    doi = _DOI_PREFIX_RE.sub("", value.strip()).strip()
    doi = doi.rstrip(".")
    return doi or None


def normalize_author_name(value: str | None) -> str:
    """One author's display form: ``"Given Family"``.

    Handles the shapes seen in the corpus:

    * ``"Wang, Joshua R."``   -> ``"Joshua R. Wang"``
    * ``"  joshua   r. wang "``-> ``"Joshua R. Wang"``
    * ``"J.R. Wang"``         -> ``"J. R. Wang"`` (initials get a space)
    * ``"Wang, J."``          -> ``"J. Wang"``
    * ``"Émile Zola"``        -> NFKC-normalised, so the accented letter
      matches the plain form used by another source.
    * ``"The ACME Institute"``-> kept verbatim: it is an organisation, and
      dropping it would misattribute a consortium paper.
    """
    name = normalize_whitespace(value)
    if not name:
        return ""
    # NFKC folds compatibility forms (full-width letters, ligatures) and
    # composes accents, so two spellings of one name compare equal.
    name = unicodedata.normalize("NFKC", name)
    name = name.replace("", "").replace("\xa0", " ")

    if "," in name:
        family, _, given = name.partition(",")
        family = normalize_whitespace(family)
        given = normalize_whitespace(given)
        name = f"{given} {family}".strip() if given else family

    # "J.R.Wang" -> "J. R. Wang": a missing space after a period glues
    # initials together, which no author search can match.
    name = re.sub(r"\.(?=[A-Z])", ". ", name)
    # "Joshua R.Wang" -> "Joshua R. Wang"
    name = re.sub(r"(?<=[a-z\)])\.(?=[A-Z])", ". ", name)
    name = normalize_whitespace(name)
    # An organisation (a consortium, a lab) has no surname; leave it alone.
    if name.isupper() or ("," in (value or "") and " " not in name.strip()):
        return name
    return name


def author_key(value: str | None) -> str:
    """A *candidate* comparison key for one author: family name + first initial.

    ``"Joshua R. Wang"`` -> ``"wang:j"``. This survives the differences that
    break exact matching: a middle name, an accented spelling, a swapped
    ``"Last, First"`` order, a missing given name.

    It is deliberately loose: two different people can share it ("Joshua Wang"
    and "Jing Wang" both give ``"wang:j"``), so it is a *candidate* key, not an
    identity. Use :func:`author_match_key` when the two must not collide.
    """
    name = normalize_author_name(value)
    if not name:
        return ""
    tokens = [t for t in re.split(r"[\s.]+", name) if t]
    if not tokens:
        return ""
    # A trailing particle belongs to the family name: "Ludwig van Beethoven".
    family = tokens[-1]
    index = len(tokens) - 1
    while index > 0 and tokens[index - 1].lower().strip(".") in _PARTICLES:
        index -= 1
        family = f"{tokens[index]} {family}"
    given = tokens[0] if index > 0 else ""
    initial = given[0].lower() if given else ""
    return f"{_PUNCT_RE.sub('', family.lower())}:{initial}"


def author_match_key(value: str | None) -> str:
    """A *strict* comparison key: family name + the first given name.

    ``"Joshua R. Wang"`` -> ``"wang:joshua"``. The middle name is dropped on
    purpose: one source writes "Joshua R. Wang" and another writes "Joshua
    Wang", and the middle initial is exactly the part that differs. Two
    people with the same family name and the same first initial differ here
    once the *given* name is spelled out, so this key decides identity where
    :func:`author_key` only narrows the candidates.
    """
    name = normalize_author_name(value)
    if not name:
        return ""
    family, given = _split_name(name)
    return f"{_PUNCT_RE.sub('', family.lower())}:{_PUNCT_RE.sub('', given.lower())}"


def _split_name(name: str) -> tuple[str, str]:
    """Split a normalised display name into (family key, first given name).

    A trailing particle belongs to the family name: "Ludwig van Beethoven" is
    family "van beethoven", given "ludwig".
    """
    tokens = [t for t in re.split(r"[\s.]+", name) if t]
    if not tokens:
        return "", ""
    family = tokens[-1]
    index = len(tokens) - 1
    while index > 0 and tokens[index - 1].lower().strip(".") in _PARTICLES:
        index -= 1
        family = f"{tokens[index]} {family}"
    given = tokens[0] if index > 0 else ""
    return family, given


def authors_match(a: str | None, b: str | None) -> bool:
    """Whether two author spellings denote the same person.

    The family name must agree. The given name must agree too, except when
    one side only carries an initial ("J. Wang" against "Joshua Wang"), where
    the initial is all the evidence there is.
    """
    name_a = normalize_author_name(a)
    name_b = normalize_author_name(b)
    if not name_a or not name_b:
        return False
    family_a, given_a = _split_name(name_a)
    family_b, given_b = _split_name(name_b)
    if _PUNCT_RE.sub("", family_a.lower()) != _PUNCT_RE.sub("", family_b.lower()):
        return False
    if not given_a or not given_b:
        # One side carries only a family name; that is the whole evidence.
        return True
    if given_a.lower() == given_b.lower():
        return True
    # One side is abbreviated to an initial: compare initials.
    if len(given_a) == 1 or len(given_b) == 1:
        return given_a[0].lower() == given_b[0].lower()
    return False


def author_keys(authors: list[str] | None) -> list[str]:
    """Comparison keys for a whole author list, de-duplicated, order kept."""
    out: list[str] = []
    seen: set[str] = set()
    for name in authors or []:
        key = author_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def normalize_title(value: str | None) -> str:
    """A title reduced to a comparison key: lowercase alphanumerics only.

    Same rule as ``harvest.normalize_title``, kept here so every collector
    agrees on one definition (and so this module can be used without importing
    the orchestrator).
    """
    if not value:
        return ""
    return _PUNCT_RE.sub("", clean_text(value).lower())


def normalize_venue_name(value: str | None) -> str:
    """A venue label reduced to a comparison key.

    The same journal is called "JMLR", "Journal of Machine Learning Research"
    and "J. Mach. Learn. Res." depending on the source. The abbreviation
    expansion is not derivable -- those spellings share no letters to key on
    -- so it is stated in :data:`VENUE_ALIASES`; everything else (case,
    punctuation, the leading "The") is folded here.
    """
    if not value:
        return ""
    text = clean_text(value).strip()
    # An abbreviation that has a known expansion compares as the expansion.
    alias = VENUE_ALIASES.get(text.lower().strip("."))
    if alias:
        text = alias
    text = re.sub(r"\b(j|of|the|and)\b", " ", text.lower())
    return re.sub(r"[^a-z0-9]+", "", text)
