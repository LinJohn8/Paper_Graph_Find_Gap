"""Cross-language query bridging: a Chinese idea must reach the English corpus."""
from __future__ import annotations

import pytest

from paper_ingest import semantics


def test_chinese_terms_are_appended_in_english():
    expanded = semantics.expand_chinese("扩散模型用于三维场景生成")
    assert "diffusion model" in expanded
    assert "3d" in expanded
    assert "scene" in expanded
    # The original text survives; only the English bridge is added.
    assert expanded.startswith("扩散模型用于三维场景生成")


def test_longest_chinese_term_wins():
    # 大语言模型 must not be reduced to 语言模型's shorter translation alone.
    assert "large language model" in semantics.expand_chinese("大语言模型")


def test_expand_chinese_is_a_no_op_for_english():
    assert semantics.expand_chinese("diffusion models for 3d generation") == "diffusion models for 3d generation"
    assert semantics.expand_chinese("") == ""


def test_expand_chinese_is_idempotent():
    once = semantics.expand_chinese("图神经网络与知识图谱")
    assert semantics.expand_chinese(once) == once


def test_query_document_bridges_then_expands_aliases():
    # 扩散 -> diffusion, and the alias layer turns "diffusion" into "diffusion model".
    doc = semantics.query_document("扩散模型")
    assert "diffusion model" in doc


def test_query_document_leaves_plain_english_alone():
    assert semantics.query_document("llm reasoning") == "large language model reasoning"


def test_unmapped_chinese_does_not_crash():
    # A sentence with no known academic term must pass through unchanged.
    assert semantics.expand_chinese("今天天气不错") == "今天天气不错"


class TestLifeSciencesVocabulary:
    """The bridge must cover the journals, not only the machine-learning papers.

    The corpus now carries PLOS ONE, eLife, Cell Reports, PNAS, Nature
    Medicine, NEJM, JAMA, BMJ and The Lancet. A Chinese query about that
    material used to reach nothing: "单细胞测序" returned 0 papers while the
    corpus held papers on single-cell sequencing, because the vocabulary
    knew only the CS terms. Measured after the addition: the same query
    returns real single-cell papers.
    """

    @pytest.mark.parametrize("chinese,expected", [
        ("单细胞测序", "single cell sequencing"),
        ("临床试验", "clinical trial"),
        ("随机对照", "randomized controlled trial"),
        ("疫苗", "vaccine"),
        ("基因组", "genome"),
        ("蛋白质", "protein"),
        ("肿瘤", "tumor"),
        ("免疫", "immune"),
        ("流行病学", "epidemiology"),
        ("药物发现", "drug discovery"),
        ("基因表达", "gene expression"),
        ("心血管", "cardiovascular"),
    ])
    def test_the_bridge_knows_the_term(self, chinese, expected):
        assert expected in semantics.expand_chinese(chinese)

    def test_the_machine_learning_vocabulary_still_works(self):
        # The life-sciences addition must not have displaced anything.
        for chinese, expected in (("图神经网络", "graph neural network"),
                                  ("强化学习", "reinforcement learning"),
                                  ("大语言模型", "large language model")):
            assert expected in semantics.expand_chinese(chinese)

    def test_no_key_is_listed_twice(self):
        """A duplicated key is silently dropped by Python; catch it here.

        Two keys ("气候", "催化") had been added twice during the same edit.
        The dictionary still worked, which is exactly why it needs a test:
        the second translation never existed and nothing complained.
        """
        import re
        from collections import Counter
        from pathlib import Path

        source = Path(semantics.__file__).read_text(encoding="utf-8")
        start = source.index("ZH_TERMS: dict[str, str] = {")
        end = source.index("\n}", start)
        keys = re.findall(r'"([^"]+)":', source[start:end])
        duplicates = {k: n for k, n in Counter(keys).items() if n > 1}
        assert not duplicates, f"duplicate ZH_TERMS keys: {duplicates}"
