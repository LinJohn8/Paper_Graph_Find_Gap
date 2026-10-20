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
