"""Tests for the shared field normalisation (paper_ingest/normalize.py).

Every case here comes from a spelling actually seen in the corpus or in a
provider payload, not from an invented example.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_ingest import normalize as n  # noqa: E402


class TestWhitespace:
    def test_collapses_and_trims(self):
        assert n.normalize_whitespace("  a   b \n c ") == "a b c"

    def test_empty_inputs(self):
        assert n.normalize_whitespace(None) == ""
        assert n.normalize_whitespace("") == ""


class TestCleanText:
    def test_strips_latex_commands_and_braces(self):
        assert n.clean_text("$O(n \\log n)$") == "O(n log n)"
        assert n.clean_text("\\emph{deep} learning") == "deep learning"

    def test_keeps_the_words(self):
        assert "attention" in n.clean_text("Attention \\textbf{Is All You Need}").lower()


class TestDoi:
    def test_all_shapes_reduce_to_the_bare_form(self):
        for raw in ("https://doi.org/10.1038/s41586-022-04599-z",
                    "http://dx.doi.org/10.1038/s41586-022-04599-z",
                    "doi:10.1038/s41586-022-04599-z",
                    "DOI: 10.1038/s41586-022-04599-z",
                    "  10.1038/s41586-022-04599-z  "):
            assert n.normalize_doi(raw) == "10.1038/s41586-022-04599-z"

    def test_rejects_non_strings(self):
        assert n.normalize_doi(None) is None
        assert n.normalize_doi(123) is None

    def test_strips_a_trailing_period(self):
        assert n.normalize_doi("10.1000/xyz.") == "10.1000/xyz"


class TestAuthorName:
    def test_last_first_is_reordered(self):
        assert n.normalize_author_name("Wang, Joshua R.") == "Joshua R. Wang"

    def test_extra_whitespace_is_collapsed(self):
        assert n.normalize_author_name("  joshua   r.  wang ") == "joshua r. wang"

    def test_glued_initials_get_a_space(self):
        assert n.normalize_author_name("J.R. Wang") == "J. R. Wang"
        assert n.normalize_author_name("Joshua R.Wang") == "Joshua R. Wang"

    def test_unicode_is_folded(self):
        # The same name written with a precomposed and a decomposed accent.
        assert n.normalize_author_name("Émile Zola") == n.normalize_author_name("Émile Zola")

    def test_organisation_is_kept_verbatim(self):
        assert n.normalize_author_name("The ACME Institute") == "The ACME Institute"

    def test_empty(self):
        assert n.normalize_author_name(None) == ""
        assert n.normalize_author_name("   ") == ""


class TestAuthorKey:
    def test_survives_the_spelling_differences(self):
        # The same researcher, three spellings, one key.
        keys = {
            n.author_key("Joshua R. Wang"),
            n.author_key("Wang, Joshua R."),
            n.author_key("J. Wang"),
        }
        assert len(keys) == 1
        assert keys.pop() == "wang:j"

    def test_particle_stays_with_the_family_name(self):
        assert n.author_key("Ludwig van Beethoven") == "vanbeethoven:l"

    def test_different_people_do_not_collide(self):
        # Same family name, same initial: the loose candidate key cannot tell
        # them apart, which is why the strict key exists.
        assert n.author_key("Joshua Wang") == n.author_key("Jing Wang")
        assert not n.authors_match("Joshua Wang", "Jing Wang")

    def test_same_person_in_two_spellings_matches(self):
        assert n.authors_match("Joshua R. Wang", "Wang, Joshua R.")
        assert n.authors_match("J. Wang", "Joshua Wang")
        assert n.authors_match("Émile Zola", "Émile Zola")

    def test_match_key_keeps_the_given_name(self):
        assert n.author_match_key("Joshua R. Wang") == "wang:joshua"
        assert n.author_match_key("Joshua R. Wang") != n.author_match_key("Jing Wang")

    def test_a_missing_given_name_still_matches(self):
        # "Wang" alone carries only the family name; that is enough.
        assert n.authors_match("Wang", "Joshua Wang")

    def test_different_families_do_not_match(self):
        assert not n.authors_match("Joshua Wang", "Joshua Li")

    def test_single_token_name_has_no_initial(self):
        assert n.author_key("Plato") == "plato:"

    def test_empty(self):
        assert n.author_key(None) == ""
        assert n.author_key("  ") == ""


class TestAuthorKeys:
    def test_deduplicates_and_keeps_order(self):
        keys = n.author_keys(["Joshua R. Wang", "Wang, Joshua R.", "Ada Lovelace"])
        assert keys == ["wang:j", "lovelace:a"]

    def test_empty_list(self):
        assert n.author_keys([]) == []
        assert n.author_keys(None) == []


class TestTitle:
    def test_punctuation_and_case_are_dropped(self):
        assert n.normalize_title("Attention Is All You Need!") == "attentionisallyouneed"
        assert n.normalize_title("DeBayes: A Bayesian Method") == n.normalize_title(
            "DeBayes - A Bayesian Method")

    def test_latex_does_not_leak_into_the_key(self):
        assert n.normalize_title("$O(n)$ Bounds") == n.normalize_title("O(n) Bounds")

    def test_empty(self):
        assert n.normalize_title(None) == ""


class TestVenueName:
    def test_known_abbreviations_expand(self):
        # "J. Mach. Learn. Res." shares no letters with the full title, so the
        # expansion is stated in VENUE_ALIASES rather than derived.
        assert n.normalize_venue_name("JMLR") == \
               n.normalize_venue_name("Journal of Machine Learning Research")
        assert n.normalize_venue_name("TPAMI") == \
               n.normalize_venue_name("IEEE Transactions on Pattern Analysis and Machine Intelligence")

    def test_case_and_punctuation_are_folded(self):
        assert n.normalize_venue_name("Nature Machine Intelligence") == \
               n.normalize_venue_name("nature machine intelligence")

    def test_empty(self):
        assert n.normalize_venue_name(None) == ""
