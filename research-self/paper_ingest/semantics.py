from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np


# --------------------------------------------------------------------------- #
# 1.1 Vocabulary bridging: curated academic surface-forms -> canonical concept  #
# --------------------------------------------------------------------------- #
ALIASES: dict[str, str] = {
    "llm": "large language model",
    "llms": "large language model",
    "gpt": "generative pretrained transformer",
    "bert": "bidirectional encoder representations",
    "icl": "in context learning",
    "rag": "retrieval augmented generation",
    "rlhf": "reinforcement learning from human feedback",
    "gan": "generative adversarial network",
    "rl": "reinforcement learning",
    "ssl": "self supervised learning",
    "vit": "vision transformer",
    "diffusion": "diffusion model",
    "nlp": "natural language processing",
    "cv": "computer vision",
    "gnn": "graph neural network",
    "transformer": "transformer",
    "mlp": "multilayer perceptron",
    "cnn": "convolutional neural network",
    "rnn": "recurrent neural network",
    "lstm": "long short term memory",
    "resnet": "residual network",
    "vae": "variational autoencoder",
    "clip": "contrastive language image pretraining",
    "sgd": "stochastic gradient descent",
    "federated": "federated learning",
    "few shot": "few shot learning",
    "zero shot": "zero shot learning",
    "meta learning": "meta learning",
    "automl": "automated machine learning",
    "ood": "out of distribution",
    "aigc": "ai generated content",
    "bayes": "bayesian",
    "bayesian": "bayesian",
    "attention": "attention mechanism",
    "capsule": "capsule network",
    "memristor": "memristor",
    "quantization": "quantization",
    "pruning": "pruning",
    "distillation": "knowledge distillation",
    "embedding": "embedding",
    "contrastive": "contrastive learning",
    "prompt": "prompting",
    "incontext": "in context learning",
}


# Build a single regex that matches any alias. Longer keys are tried first so
# that e.g. "llms" is matched before "llm" and "few shot" before any sub-token.
_ALIAS_PATTERN = re.compile(
    r"\b("
    + "|".join(re.escape(k) for k in sorted(ALIASES, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)


def _build_expansion() -> tuple[re.Pattern[str], dict[str, str]]:
    """Build an idempotent alias-expansion regex plus its replacement table.

    A naive ``re.sub`` over ``ALIASES`` is not idempotent because some canonical
    forms *contain* their own alias:

        "diffusion"   -> "diffusion model"   ("diffusion model" -> "diffusion model model")
        "attention"   -> "attention mechanism"
        "few shot"    -> "few shot learning"
        "prompt"      -> "prompting"
        "capsule"     -> "capsule network"
        "distillation"-> "knowledge distillation"

    Running the substitution twice therefore stacked copies ("model model
    model"), and that artifact was visibly leaking into real cluster labels.

    Fix: build ONE alternation where the longest known phrases win, and add the
    self-referential canonical forms themselves as *protected* (no-op) branches.
    ``re.sub`` scans left-to-right and never overlaps, so in "diffusion model"
    the protected branch matches the whole phrase and "diffusion" never gets a
    chance to append a second "model". Applying the result again is then a
    no-op, i.e. the transformation is idempotent.
    """
    replace: dict[str, str] = {}
    protected: set[str] = set()
    for alias, canonical in ALIASES.items():
        alias_l = alias.lower()
        canonical_l = canonical.lower()
        if alias_l == canonical_l:
            continue  # identity alias: nothing to do
        replace[alias_l] = canonical_l

    # Every canonical phrase is itself a fixed point of the vocabulary. Without
    # protecting whole phrases, a canonical form can be re-expanded through a
    # *different* alias on a second pass: "clip" -> "contrastive language image
    # pretraining" would become "... contrastive learning ..." because
    # "contrastive" is also a key. Protecting the whole phrase (longest-first)
    # leaves it untouched, which is what makes the mapping idempotent.
    for canonical_l in replace.values():
        protected.add(canonical_l)

    # Longest first so "few shot learning" beats "few shot" and "llm" loses to "llms".
    phrases = sorted(set(replace) | protected, key=len, reverse=True)
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(p) for p in phrases) + r")\b",
        re.IGNORECASE,
    )
    return pattern, replace


_EXPAND_PATTERN, _EXPANSION_MAP = _build_expansion()


# --------------------------------------------------------------------------- #
# 1.1b Cross-language query terms (Chinese -> English academic surface forms)   #
# --------------------------------------------------------------------------- #
# The corpus is English; a reader typing Chinese in the idea finder previously
# got zero query terms in the vocabulary, so every search degraded to keyword
# matching and returned little. These are *query-side* bridges only: they are
# applied to what the user types, never to stored documents, so the fitted model
# and existing vectors stay valid. Longest key first so "大语言模型" wins over
# "语言模型" and "图神经网络" over "神经网络".
ZH_TERMS: dict[str, str] = {
    "大语言模型": "large language model", "语言模型": "language model",
    "大模型": "large language model", "多模态": "multimodal",
    "深度学习": "deep learning", "机器学习": "machine learning",
    "强化学习": "reinforcement learning", "自监督": "self supervised learning",
    "半监督": "semi supervised", "无监督": "unsupervised", "监督学习": "supervised learning",
    "迁移学习": "transfer learning", "元学习": "meta learning", "联邦学习": "federated learning",
    "持续学习": "continual learning", "增量学习": "incremental learning",
    "对比学习": "contrastive learning", "表示学习": "representation learning",
    "图神经网络": "graph neural network", "神经网络": "neural network",
    "卷积": "convolutional", "注意力机制": "attention mechanism", "注意力": "attention",
    "变换器": "transformer", "扩散模型": "diffusion model", "扩散": "diffusion",
    "生成对抗": "generative adversarial network", "生成模型": "generative model",
    "生成": "generation", "重建": "reconstruction", "分割": "segmentation",
    "检测": "detection", "识别": "recognition", "检索": "retrieval",
    "推荐系统": "recommender system", "推荐": "recommendation",
    "知识图谱": "knowledge graph", "知识蒸馏": "knowledge distillation",
    "蒸馏": "distillation", "量化": "quantization", "剪枝": "pruning",
    "压缩": "compression", "推理": "inference", "训练": "training",
    "微调": "fine tuning", "预训练": "pretraining", "提示学习": "prompting",
    "提示": "prompt", "对齐": "alignment", "幻觉": "hallucination",
    "可解释": "interpretability", "鲁棒": "robustness", "鲁棒性": "robustness",
    "公平性": "fairness", "隐私": "privacy", "安全": "security",
    "对抗攻击": "adversarial attack", "对抗": "adversarial",
    "三维": "3d", "场景": "scene", "点云": "point cloud", "渲染": "rendering",
    "图像": "image", "视频": "video", "语音": "speech", "音频": "audio",
    "文本": "text", "自然语言处理": "natural language processing",
    "计算机视觉": "computer vision", "机器翻译": "machine translation",
    "目标检测": "object detection", "图像分割": "image segmentation",
    "姿态估计": "pose estimation", "动作识别": "action recognition",
    "时间序列": "time series", "预测": "prediction", "优化": "optimization",
    "采样": "sampling", "采样效率": "sample efficiency", "效率": "efficiency",
    "加速": "acceleration", "并行": "parallel", "分布式": "distributed",
    "贝叶斯": "bayesian", "概率": "probabilistic", "不确定性": "uncertainty",
    "因果": "causal", "推理能力": "reasoning", "推理链": "chain of thought",
    "思维链": "chain of thought", "智能体": "agent", "多智能体": "multi agent",
    "工具调用": "tool use", "规划": "planning", "决策": "decision making",
    "机器人": "robot", "具身智能": "embodied ai", "自动驾驶": "autonomous driving",
    "医学图像": "medical image", "生物信息": "bioinformatics", "分子": "molecule",
    "药物": "drug", "蛋白质": "protein", "材料": "materials", "气候": "climate",
    "能源": "energy", "金融": "financial", "教育": "education",
    "评估": "evaluation", "基准": "benchmark", "数据集": "dataset",
    "泛化能力": "generalization", "泛化": "generalization", "过拟合": "overfitting",
    "稀疏": "sparse", "稠密": "dense", "轻量化": "lightweight", "模型压缩": "model compression",
    "边缘计算": "edge computing", "长文本": "long context", "上下文": "context",
    "记忆": "memory", "检索增强": "retrieval augmented generation",
    "问答": "question answering", "摘要": "summarization", "翻译": "translation",
    "情感分析": "sentiment analysis", "命名实体": "named entity",
    "语音识别": "speech recognition", "合成": "synthesis", "编辑": "editing",
    "风格迁移": "style transfer", "超分辨率": "super resolution", "去噪": "denoising",
}

_ZH_PATTERN = re.compile("|".join(re.escape(k) for k in sorted(ZH_TERMS, key=len, reverse=True)))


def expand_chinese(text: str) -> str:
    """Append English equivalents of any Chinese academic terms in ``text``.

    The original text is kept (harmless: its characters match no vocabulary term)
    and the English surface forms are appended, so the query can reach the English
    corpus. Idempotent: running it again finds the same terms and appends the same
    suffix, which is then already present, so the result stops changing.
    """
    if not text or not re.search(r"[\u4e00-\u9fff]", text):
        return text
    found: list[str] = []

    def swap(match: re.Match[str]) -> str:
        english = ZH_TERMS[match.group(0)]
        if english not in found:
            found.append(english)
        return match.group(0)

    _ZH_PATTERN.sub(swap, text)
    if not found:
        return text
    # Only add terms that are not already in the text, so a second pass is a no-op.
    lowered = text.lower()
    missing = [e for e in found if e not in lowered]
    if not missing:
        return text
    return text + " " + " ".join(missing)


def query_document(text: str) -> str:
    """Normalise a *query* string: Chinese bridge, then alias expansion.

    Documents are indexed with :func:`expand_aliases` only. Query-side extra
    bridging is safe because it adds terms to the query vector without changing
    the fitted vector space.
    """
    return expand_aliases(expand_chinese(text))


def expand_aliases(text: str) -> str:
    """Token-level, word-boundary-safe alias expansion (case-insensitive).

    The expansion is **idempotent**: ``expand_aliases(expand_aliases(x))`` equals
    ``expand_aliases(x)`` for every input, so canonical phrases are never doubled
    no matter how many times a document passes through the pipeline.
    """
    if not text:
        return ""
    return _EXPAND_PATTERN.sub(lambda m: _EXPANSION_MAP.get(m.group(0).lower(), m.group(0)), text)


# --------------------------------------------------------------------------- #
# 1.2 Weighted document construction                                           #
# --------------------------------------------------------------------------- #
def make_doc(title: str, keywords: list[str] | None, abstract: str | None) -> str:
    """Sharpen topic resolution by over-weighting the title and keywords.

    (title + " ") * 3 + (" ".join(keywords) + " ") * 2 + (abstract or "")
    """
    title = (title or "").strip()
    kw = " ".join(keywords) if keywords else ""
    kw = kw.strip()
    abstract = abstract or ""
    return (title + " ") * 3 + (kw + " ") * 2 + abstract


# --------------------------------------------------------------------------- #
# 1.3 Richer TF-IDF + SVD semantic model                                       #
# --------------------------------------------------------------------------- #
class SemanticModel:
    """Deterministic TF-IDF + TruncatedSVD embedding, persisted with joblib."""

    def __init__(self, dims: int) -> None:
        self.dims = dims
        self.vectorizer = None
        self.svd = None
        self.feature_names_: np.ndarray | None = None

    def fit(self, corpus: list[str]) -> "SemanticModel":
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer

        corpus = [c or "" for c in corpus]
        n_samples = len(corpus)
        # `min_df=2` drops every term seen only once. That is fine for large
        # corpora but catastrophic on small ones (a 1-3 paper corpus would get an
        # empty vocabulary) and it also removes rare-but-precise query terms.
        # `max_df=0.5` similarly throws away the most common terms, which on tiny
        # corpora is most of the vocabulary. Scale both with the corpus size.
        min_df = 2 if n_samples >= 50 else 1
        max_df = 0.5 if n_samples >= 20 else 1.0

        def _make_vectorizer(min_df_: int, max_df_: float, permissive: bool = False):
            opts: dict[str, Any] = dict(
                max_features=1024,
                stop_words="english",
                ngram_range=(1, 2),
                sublinear_tf=True,
                min_df=min_df_,
                max_df=max_df_,
                strip_accents="unicode",
            )
            if permissive:
                # Last-resort configuration: keep every token, no stop-word list,
                # unigrams only, so even a one-word corpus yields a vocabulary.
                opts.update(
                    stop_words=None, ngram_range=(1, 1),
                    token_pattern=r"(?u)\b\w+\b",
                )
            return TfidfVectorizer(**opts)

        vectorizer = _make_vectorizer(min_df, max_df)
        try:
            matrix = vectorizer.fit_transform(corpus)
        except ValueError:
            # Degenerate corpora (empty strings, only stop-words, a single
            # character document) raise "empty vocabulary". Retry with the most
            # permissive configuration, then degrade to an unfitted model rather
            # than raising: callers fall back to keyword search.
            try:
                vectorizer = _make_vectorizer(1, 1.0, permissive=True)
                matrix = vectorizer.fit_transform(corpus)
            except ValueError:
                self.vectorizer = None
                self.svd = None
                self.feature_names_ = None
                return self

        if matrix.shape[1] == 0 or matrix.shape[0] == 0 or matrix.shape[1] < 2:
            # Nothing meaningful to decompose. TruncatedSVD also requires at
            # least 2 features, so a single-token corpus (["transformer"]) must
            # degrade here instead of raising. Callers fall back to keyword
            # search when the model is unfitted.
            self.vectorizer = None
            self.svd = None
            self.feature_names_ = None
            return self

        self.vectorizer = vectorizer
        n_features = matrix.shape[1]
        n_samples = matrix.shape[0]
        # SVD components must be < n_features and < n_samples, and at least 2.
        comps = min(self.dims, max(2, n_features - 1), max(2, n_samples - 1))
        comps = max(2, comps)
        self.svd = TruncatedSVD(n_components=comps, random_state=17)
        self.svd.fit_transform(matrix)
        self.feature_names_ = self.vectorizer.get_feature_names_out()
        return self

    @property
    def out_dims(self) -> int:
        """Actual embedding width produced by :meth:`transform`.

        ``fit`` clamps the requested ``dims`` to the data (``comps`` may be
        smaller than ``dims`` on tiny corpora). Callers such as the zero-vector
        fallback must use this width, otherwise they emit arrays of the wrong
        shape and any ``vectors @ vectors.T`` downstream breaks.
        """
        if self.svd is not None and hasattr(self.svd, "components_"):
            return int(self.svd.components_.shape[0])
        return int(self.dims)

    def transform(self, texts: list[str]) -> np.ndarray:
        if self.vectorizer is None or self.svd is None:
            # Unfitted model: return the correct, *fitted* width where known.
            return np.zeros((len(texts), self.out_dims), dtype=float)
        tfidf = self.vectorizer.transform(texts)
        svd = self.svd.transform(tfidf)
        svd = np.asarray(svd, dtype=float)
        norms = np.linalg.norm(svd, axis=1, keepdims=True)
        svd = svd / (norms + 1e-12)
        return svd

    def save(self, path: Path) -> None:
        import joblib

        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            joblib.dump(self, path)
        except Exception:
            # Best-effort persistence; never break the caller.
            pass

    @classmethod
    def load(cls, path: Path) -> "SemanticModel | None":
        import joblib

        try:
            obj = joblib.load(path)
            if isinstance(obj, SemanticModel):
                return obj
            return None
        except Exception:
            return None

    def top_terms_per_component(self, k: int = 10) -> dict[int, list[tuple[str, float]]]:
        if self.svd is None or self.feature_names_ is None:
            return {}
        out: dict[int, list[tuple[str, float]]] = {}
        for i, comp in enumerate(self.svd.components_):
            order = np.argsort(comp)[::-1][:k]
            out[i] = [(str(self.feature_names_[j]), float(comp[j])) for j in order]
        return out


# --------------------------------------------------------------------------- #
# 1.4 Meaningful topic labels                                                  #
# --------------------------------------------------------------------------- #
# Tokens that carry no topical signal but survive english stop-word removal
# (they are common in *abstracts*, not in the stop-word list). On real data
# these dominated labels ("aim · approximations · assuming").
_LABEL_STOPWORDS = frozenset(
    """
    aim aims aiming approach approaches assume assumed assumes assuming assumption
    result results resulting show shows shown showing propose proposes proposed
    present presents presented introduce introduces introduced develop develops
    developed study studies studied investigate investigates investigated
    method methods methodology model models modeling modelling framework frameworks
    paper papers work works we our this these those that which using used use uses
    based via towards toward however moreover furthermore therefore thus also
    one two three first second third new novel recent current existing different
    various several many much more most less least such same other others
    """.split()
)


def _is_usable_label_term(term: str) -> bool:
    """Reject tokens that make a label look like noise."""
    t = term.strip().lower()
    if not t:
        return False
    # Drop pure punctuation / digits / very short fragments.
    if len(t.replace(" ", "")) < 3:
        return False
    if all(ch.isdigit() or not ch.isalnum() for ch in t):
        return False
    core = t.replace(" ", "_")
    if core in _LABEL_STOPWORDS:
        return False
    # A bigram made only of filler words ("aim assumptions") is noise too.
    parts = [p for p in t.split() if p]
    if parts and all(p in _LABEL_STOPWORDS for p in parts):
        return False
    return True


def label_cluster(cluster_term_matrix: dict[str, float], global_mean: dict[str, float],
                  top_k: int = 3, cluster_size: int = 1,
                  cluster_doc_freq: dict[str, int] | None = None) -> str:
    """Label a cluster with its most *distinctive and well-supported* terms.

    The previous scoring (``within / global``) was degenerate: any term absent
    from the global mean scored ``within * 1e6``, so one accidental rare token
    always outranked genuinely representative terms, and filler words like
    "aim"/"assuming" won because abstracts are full of them. On real data the
    labels came out as "aim · approximations · assuming".

    New scoring keeps the distinctiveness ratio (that is what makes a label
    *identify* a cluster) but:

    * requires a minimum share of the cluster's documents to contain the term
      (so a single weird paper cannot name a whole cluster),
    * multiplies by a saturating absolute-weight factor so a term that is
      distinctive but negligible in the cluster cannot outrank a main topic,
    * filters filler/stopword/numeric fragments outright.
    """
    scored: list[tuple[float, str]] = []
    for term, within in cluster_term_matrix.items():
        if not _is_usable_label_term(term):
            continue
        if within <= 0:
            continue
        g = global_mean.get(term, 0.0)
        # Distinctiveness: how much more this cluster uses the term than average.
        if g > 0:
            ratio = within / g
        else:
            # Term only in this cluster: distinctive, but bounded so it cannot
            # mathematically dominate every well-supported term.
            ratio = 50.0
        # Support guard: term must appear in a reasonable share of the cluster.
        if cluster_doc_freq is not None and cluster_size > 0:
            share = cluster_doc_freq.get(term, 0) / max(1, cluster_size)
            if share < 0.2 and cluster_size >= 5:
                continue
            support = min(1.0, 0.5 + share)
        else:
            support = 1.0
        # Absolute weight factor in [0,1): keeps relative magnitudes meaningful.
        abs_factor = within / (within + 1.0)
        scored.append((ratio * support * abs_factor, term))
    scored.sort(key=lambda x: (-x[0], x[1]))
    # Drop n-gram echoes: "large language", "language model" and "large language
    # model" all describe one topic, so keep the strongest then skip any term
    # whose tokens are a subset of an already-selected term (or vice versa).
    selected: list[str] = []
    selected_tokens: list[set[str]] = []
    for _, term in scored:
        tokens = set(term.lower().split())
        if any(tokens <= st or st <= tokens for st in selected_tokens):
            continue
        selected.append(term)
        selected_tokens.append(tokens)
        if len(selected) >= top_k:
            break
    if not selected:
        return ""
    return " · ".join(selected)


def label_clusters_from_tfidf(tfidf_matrix: Any, labels: Any,
                              feature_names: Any, top_k: int = 3) -> dict[int, str]:
    """Build human-readable labels per cluster from a TF-IDF matrix."""
    labels = np.asarray(labels)
    n_features = tfidf_matrix.shape[1]
    gm = tfidf_matrix.mean(axis=0)
    if hasattr(gm, "toarray"):
        gm = gm.toarray()
    global_mean = np.asarray(gm).ravel()
    global_mean_dict = {feature_names[j]: float(global_mean[j]) for j in range(n_features)}

    out: dict[int, str] = {}
    for c in np.unique(labels):
        c = int(c)
        idx = np.where(labels == c)[0]
        if len(idx) == 0:
            continue
        cs = tfidf_matrix[idx].sum(axis=0)
        if hasattr(cs, "toarray"):
            cs = cs.toarray()
        cluster_sum = np.asarray(cs).ravel()
        cluster_dict = {feature_names[j]: float(cluster_sum[j]) for j in range(n_features)}
        # Document frequency *within* the cluster, used as a support guard.
        sub = tfidf_matrix[idx]
        doc_freq = np.asarray((sub > 0).sum(axis=0)).ravel()
        cluster_doc_freq = {feature_names[j]: int(doc_freq[j]) for j in range(n_features)}
        out[c] = label_cluster(
            cluster_dict, global_mean_dict, top_k=top_k,
            cluster_size=len(idx), cluster_doc_freq=cluster_doc_freq,
        )
    return out


# --------------------------------------------------------------------------- #
# 1.5 Research-question (RQ) extraction                                        #
# --------------------------------------------------------------------------- #
# Genuine question / open-problem framings: these describe what is NOT yet known,
# which is what actually helps a user invent a new idea.
_RQ_OPEN_RE = re.compile(
    r"we (?:aim|seek|attempt|try) to|we ask (?:whether|how|if|why)|"
    r"we (?:investigate|study|examine|explore) whether|"
    r"whether (?:there|it|the|current|existing|we)|"
    r"open (?:question|problem|issue|direction)s?|remains? (?:an? )?(?:open|unclear|unknown)|"
    r"it (?:is|remains) (?:unclear|unknown|an open)|"
    r"to (?:answer|address) (?:this|the) (?:question|open problem)|"
    r"\bquestion is\b|we address the question|"
    r"(?:can|could|does|do|how|why|what) [^.?]{0,120}\?",
    re.IGNORECASE,
)

# Pure contribution / claim statements. On real data 84% of papers matched these,
# which made "Research questions" read like a contribution list ("we introduce X").
# They are kept as a LAST-RESORT fallback only.
_RQ_CONTRIB_RE = re.compile(
    r"we propose|we present|we introduce|we develop|we design|we construct|"
    r"our contribution|our contributions|"
    r"this paper (?:proposes|introduces|presents|develops|describes)|"
    r"we show that|we demonstrate|we prove that|we release|we build",
    re.IGNORECASE,
)


class _RqText(str):
    """A sentence plus the priority class that produced it."""


def _classify_rq_sentence(sentence: str) -> int:
    """Return 0 for a real open question, 1 for a contribution claim, -1 else."""
    if "?" in sentence:
        return 0
    if _RQ_OPEN_RE.search(sentence):
        return 0
    if _RQ_CONTRIB_RE.search(sentence):
        return 1
    return -1


def _split_sentences(text: str) -> list[str]:
    text = (text or "").replace("\n", " ")
    # Splitting on [.!?] would consume the delimiter, so we re-attach it so that
    # question sentences still *contain* "?" for the RQ/ gap detectors.
    parts = re.split(r"([.!?])", text)
    out: list[str] = []
    for i in range(0, len(parts) - 1, 2):
        sentence = parts[i]
        punct = parts[i + 1] if i + 1 < len(parts) else ""
        out.append(sentence + punct)
    if len(parts) % 2 == 1 and parts[-1].strip():
        out.append(parts[-1])
    return out


def _cap(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    trunc = text[:max_len]
    space = trunc.rfind(" ")
    if space > 0:
        trunc = trunc[:space]
    return trunc.rstrip() + "\u2026"


def extract_rqs(abstract: str, max_len: int = 240) -> list[str]:
    """Extract genuine *research questions* / open problems from an abstract.

    Why this is not just "sentences with a cue": on the real 400-paper ICML 2024
    corpus the old cue list (``we propose|we present|we introduce|...``) fired on
    84% of papers and almost every hit was a *contribution statement*
    ("we introduce Charmer", "This paper presents InferCept"). For the product
    goal -- brainstorming a new idea and finding the open questions it could
    answer -- those are noise.

    Priority order:
      1. open-question framings ("?", "we ask whether", "remains an open
         problem", "whether ...", "the question is", ...),
      2. contribution claims, used ONLY as a fallback when a paper states no
         genuine open question at all (a paper still needs *something* to show).

    Returns at most a handful of sentences, de-duplicated case-insensitively and
    trimmed, capped to ``max_len`` characters each.
    """
    open_hits: list[str] = []
    contrib_hits: list[str] = []
    seen: set[str] = set()
    for raw in _split_sentences(abstract):
        s = re.sub(r"\s+", " ", raw).strip()
        if not s or len(s) < 20:
            continue
        kind = _classify_rq_sentence(s)
        if kind < 0:
            continue
        capped = _cap(s, max_len)
        key = capped.lower()
        if key in seen:
            continue
        seen.add(key)
        (open_hits if kind == 0 else contrib_hits).append(capped)
    if open_hits:
        return open_hits
    # No real open question found: fall back to contribution claims so the UI is
    # not empty, but at most two (they are the weakest signal).
    return contrib_hits[:2]


# --------------------------------------------------------------------------- #
# 1.5b Derived research questions (turn stated gaps into answerable questions)  #
# --------------------------------------------------------------------------- #
# The product goal is not "quote the paper's gap sentence" but "give the user a
# question they could try to answer". Most abstracts state a gap in declarative
# form ("little attention has been given to X", "Y remains unexplored") or as a
# negative result ("existing methods fail to handle Z"). These helpers rewrite
# that into an interrogative research question.
_GAP_TO_QUESTION = [
    # (pattern, replacement template using named groups)
    # Each `x` is a *short* noun phrase: bounded and stopped at clause
    # punctuation so a greedy match cannot swallow a whole clause into the
    # question ("... perturbations often hampers their deployment be ...").
    (re.compile(r"little (?:attention|work|research) (?:has been |is )?(?:given|paid|devoted)? ?to (?P<x>[^,;]{3,90})[.;]?$", re.I),
     "To what extent can {x} be addressed?"),
    (re.compile(r"(?P<x>[^,;]{3,90}?) (?:remains?|is) (?:an? )?(?:open|unexplored|under-?explored|unclear|unknown)(?: (?:problem|question|issue|direction))?[.;]?$", re.I),
     "How can {x} be characterized or solved?"),
    (re.compile(r"(?:has|have) (?:not|never) been (?:explored|studied|addressed|investigated)[: ]*(?P<x>[^,;]{3,90})?[.;]?$", re.I),
     "What happens when {x} is studied directly?"),
    (re.compile(r"(?:existing|current|prior|previous) (?:methods?|approaches?|models?|work) (?:fail|struggle|are unable) to (?P<x>[^,;]{3,90})[.;]?$", re.I),
     "How can models reliably {x}?"),
    (re.compile(r"lack(?:s|ing)? of (?P<x>[^,;]{3,90})[.;]?$", re.I),
     "How can {x} be guaranteed or measured?"),
    (re.compile(r"limited (?:understanding|study|studies|evidence) (?:of|on|about) (?P<x>[^,;]{3,90})[.;]?$", re.I),
     "What do we still not understand about {x}?"),
    (re.compile(r"few studies (?:have )?(?:examined|investigated|addressed)? ?(?P<x>[^,;]{3,90})[.;]?$", re.I),
     "What would a systematic study of {x} reveal?"),
    (re.compile(r"under-?explored[: ]+(?P<x>[^,;]{3,90})[.;]?$", re.I),
     "What is the unexplored potential of {x}?"),
    # Real-corpus phrasings that the first template set missed.
    (re.compile(r"remains? challenging[^.]*?(?:for|in|to) (?P<x>[^,;]{3,90})[.;]?$", re.I),
     "How can {x} be made more tractable?"),
    (re.compile(r"(?P<x>[^,;]{3,90}?) (?:have|has) (?:not|never) been (?:well )?(?:understood|characterized|quantified)[.;]?$", re.I),
     "How can {x} be better understood?"),
    (re.compile(r"(?:we|the authors) (?:leave|left) (?P<x>[^,;]{3,90}) (?:to|for) future work[.;]?$", re.I),
     "What would it take to solve {x}?"),
    (re.compile(r"(?:an?|the) (?:important|promising|open) (?:direction|question|problem) (?:is|for|in) (?P<x>[^,;]{3,90})[.;]?$", re.I),
     "What progress is possible on {x}?"),
    (re.compile(r"(?:there is|there remains?) (?:still )?(?:no|a lack of) (?P<x>[^,;]{3,90})[.;]?$", re.I),
     "What is needed to provide {x}?"),
]


def _extract_gap_object(cue: str) -> str:
    """Pull the noun-ish object out of a gap sentence as a fallback question subject.

    "However, evaluating them comprehensively remains challenging." -> the clause
    before the shortfall marker ("evaluating them comprehensively"), cleaned.
    Works on the text *before* the gap marker so we get the thing that is missing
    rather than the complaint itself.
    """
    # Cut at the first shortfall marker and keep what precedes it.
    marker = re.search(
        r"\b(?:remains?|is|are)\b.*\b(?:open|challenging|unclear|unknown|difficult)\b|"
        r"\b(?:has|have) (?:not|never) been\b|"
        r"\b(?:little|limited|few|scarce|rarely|overlooked|neglected)\b|"
        r"\b(?:lack|lacks|lacking)\b",
        cue, re.IGNORECASE,
    )
    head = cue[: marker.start()] if marker else cue
    head = re.sub(r"^\s*(?:despite|although|though|while|however|yet|but|and)\b[, ]*", "", head, flags=re.I)
    head = _clean_question_fragment(head)
    if len(head.split()) < 3:
        return ""
    return head


def _clean_question_fragment(fragment: str) -> str:
    """Trim a clause so it reads well inside a generated question."""
    f = re.sub(r"\s+", " ", fragment).strip(" .,;:")
    # Drop leading connectives carried over from the source sentence.
    f = re.sub(r"^(?:that|which|whether|if|the fact that|however,?|but|and)\s+", "", f, flags=re.I)
    # Drop trailing subordinate clauses that make the question unreadable.
    f = re.split(r",?\s*(?:which|while|whereas|but|although|though)\s+", f, maxsplit=1)[0]
    return f.strip(" .,;:")


def _fragment_is_usable(fragment: str) -> bool:
    """Reject fragments that would produce a broken or vacuous question.

    Real corpora produce things like "rigorously is studied directly" or a whole
    background clause; neither makes a sensible question. But the bar must not be
    so high that perfect gap phrasings are thrown away ("incorporating
    symmetries"), so we only reject clearly-unusable shapes.
    """
    f = fragment.strip()
    if len(f) < 10 or len(f) > 160:
        return False
    words = f.split()
    if len(words) < 2:
        return False
    content = [w for w in words if len(w) >= 4 and w.lower() not in _LABEL_STOPWORDS]
    if not content:
        return False
    if re.match(r"^(?:rigorously|often|always|never|usually|recently|currently|"
                r"typically|generally|particularly|especially)\b", f, re.I):
        return False
    if re.search(r"\b(?:their|its|his|her)\b", f, re.I):
        # Carrying a possessive pronoun means the fragment references a previous
        # clause and cannot stand alone as a question subject. (Plain "these"/
        # "those" are fine: "these models generalize to new domains" is a valid
        # question subject.)
        return False
    # Reject fragments that still contain a stranded auxiliary or a clause
    # boundary: they came from a greedy match that swallowed part of a sentence.
    if re.search(r"\b(?:has|have|had|was|were|been|be)\s+(?:only|also|still|already)\b", f, re.I):
        return False
    if re.search(r"\b(?:which|who|whose|where|when|while|although|though|"
                r"because|since|however|moreover|furthermore)\b", f, re.I):
        return False
    # A finite verb inside the fragment means we captured a whole clause rather
    # than the thing being asked about.
    if re.search(r"\b(?:is|are|was|were|hampers?|fails?|lacks?|requires?)\b", f, re.I):
        return False
    # A fragment must not end with a function word (it was cut mid-clause).
    if words[-1].lower() in {
        "the", "a", "an", "of", "to", "in", "on", "for", "with", "by", "and",
        "or", "but", "as", "at", "from", "into", "than", "that", "which",
    }:
        return False
    return True


def derive_research_questions(abstract: str, max_len: int = 240,
                              limit: int = 3) -> list[str]:
    """Turn stated gaps / limitations in an abstract into *answerable questions*.

    This is the layer that actually supports "brainstorm an idea and find new
    possibilities": a quoted gap sentence ("little attention has been given to
    symmetries in TNPs") is a dead end, while "To what extent can symmetries in
    TNPs be addressed?" is a prompt for new work.

    Only genuine gap sentences are rewritten -- contribution claims are never
    turned into fake questions -- and fragments that would yield a broken
    question are skipped rather than pasted into a generic template. Returns an
    empty list when nothing qualifies.
    """
    out: list[str] = []
    seen: set[str] = set()
    for cue in extract_gap_cues(abstract, max_len=max_len * 2, limit=limit * 4):
        produced = None
        for pattern, template in _GAP_TO_QUESTION:
            m = pattern.search(cue)
            if not m or "x" not in (m.groupdict() or {}):
                continue
            frag = _clean_question_fragment(m.group("x") or "")
            if not _fragment_is_usable(frag):
                continue
            produced = template.format(x=frag)
            break
        # No template matched: skip. A generic "How can we address the open
        # issue of <raw clause>?" wrapper produced unusable questions on the real
        # corpus ("... of Our main result?", "... of the problem has been studied
        # ... much?"), which is worse than nothing for an idea-generation tool.
        if produced is None:
            continue
        produced = _cap(produced, max_len)
        if not produced:
            continue
        key = produced.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(produced)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# 1.6 Research-gap (RG) detection                                              #
# --------------------------------------------------------------------------- #
# Strong, unambiguous gap framings (a knowledge/coverage shortfall).
_GAP_STRONG_RE = re.compile(
    r"remains? (?:an )?open|open (?:problem|question|issue|challenge|direction)s?|"
    r"under[- ]explored|under[- ]studied|understudied|"
    r"little (?:attention|work|research|is known)|"
    r"(?:has|have) (?:not|never) been (?:explored|studied|addressed|investigated)|"
    r"remains? (?:unexplored|unknown|unclear|challenging|difficult)|"
    r"not well (?:understood|studied)|poorly understood|"
    r"few studies|limited (?:understanding|study|studies|attention|work|evidence)|"
    r"future work|open directions?|yet to be (?:explored|studied|addressed)|"
    r"we leave .{0,40}for future work|"
    r"calls? for (?:further|more)|important (?:direction|open)|promising direction|"
    r"scarce|sparse literature|rarely studied|overlooked|neglected|"
    r"no (?:existing|prior|known) (?:method|work|approach|study)",
    re.IGNORECASE,
)

# Weak framings: only count when the sentence is clearly about a research
# limitation rather than ordinary background. "their lack of robustness ...
# hampers deployment" is motivation, not a gap statement.
_GAP_WEAK_RE = re.compile(
    r"lack(?:s|ing)? of|fail(?:s|ed)? to|cannot handle|struggles? to|"
    r"does not address|do not address|difficult to|"
    r"limited (?:by|to)|no (?:guarantee|theory|analysis)",
    re.IGNORECASE,
)

# The sentence must be *about* a research object for a weak cue to count.
_GAP_OBJECT_RE = re.compile(
    r"method|model|approach|algorithm|framework|study|studies|analysis|"
    r"understanding|theory|theoretical|benchmark|dataset|evaluation|"
    r"literature|research|work|paper|guarantee|setting|regime|architecture|"
    r"technique|solution|problem|question|direction",
    re.IGNORECASE,
)

# Sentences that open with a contrast/limitation discourse marker are much more
# likely to be genuine gap statements than mid-paragraph background.
_GAP_DISCOURSE_RE = re.compile(
    r"^\s*(?:despite|although|though|while|however|yet|still|"
    r"there (?:is|are|remains?)|current(?:ly)?|existing|prior|previous|"
    r"most (?:existing|prior|current)|in contrast|unlike)",
    re.IGNORECASE,
)


def _is_gap_sentence(sentence: str) -> bool:
    """Decide whether a sentence states a research gap (precision-guarded)."""
    s = sentence.strip()
    if len(s) < 30:
        return False
    if _GAP_STRONG_RE.search(s):
        return True
    if _GAP_WEAK_RE.search(s):
        # Weak cue: require both a research object and (usually) a contrast
        # marker so plain background lines are rejected.
        return bool(_GAP_OBJECT_RE.search(s)) and bool(_GAP_DISCOURSE_RE.search(s))
    return False


def extract_gap_cues(text: str, max_len: int = 240, limit: int = 10) -> list[str]:
    """Return sentences in `text` that state a research gap / open problem.

    Recall was very low on real abstracts (only ~5% of papers matched) and the
    ``lack of`` branch also fired on plain background sentences. The strong-cue
    list is broadened to real paper phrasing, while the weak cues now require a
    research object and a contrast marker.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in _split_sentences(text):
        s = re.sub(r"\s+", " ", raw).strip()
        if not s or not _is_gap_sentence(s):
            continue
        capped = _cap(s, max_len)
        key = capped.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(capped)
        if len(out) >= limit:
            break
    return out


def _paper_entry(pid: str, title: str, abstract: str) -> dict[str, str]:
    return {"id": pid, "title": title, "abstract": abstract or ""}


def detect_gaps(vectors: np.ndarray | None, labels: np.ndarray, abstracts: list[str],
                ids: list[str], titles: list[str], top_terms: dict[int, list[str]],
                bridge_sim: float | None = None, sparse_size: int = 8,
                sparse_intra: float = 0.6) -> list[dict]:
    """Detect bridge / sparse / cue research-gap candidates.

    ``bridge_sim`` is the minimum centroid cosine for two clusters to be
    considered "conceptually close". It defaults to ``None`` meaning *adaptive*:
    an absolute 0.6 was unreachable on real TF-IDF/SVD data (measured max
    inter-cluster centroid cosine over 8 real ICML clusters was 0.594), so the
    bridge detector silently produced zero results no matter how related the
    fields were. The adaptive threshold is derived from the observed pairwise
    centroid distribution, so genuine close-but-uncombined pairs are surfaced.

    Each gap dict: {type, title, explanation, score, paper_ids, papers}.
    """
    gaps: list[dict] = []
    labels = np.asarray(labels)
    ids = list(ids)
    titles = list(titles)
    abstracts = list(abstracts)
    n = len(ids)

    meta = {ids[i]: _paper_entry(ids[i], titles[i], abstracts[i]) for i in range(n)}

    members: dict[int, list[int]] = {}
    for i, lab in enumerate(labels):
        members.setdefault(int(lab), []).append(i)

    # ---- bridge ---------------------------------------------------------- #
    if vectors is not None and len(vectors) > 0:
        vectors = np.asarray(vectors, dtype=float)
        centroids: dict[int, np.ndarray] = {}
        for c, idx in members.items():
            if idx:
                cent = vectors[idx].mean(axis=0)
                norm = np.linalg.norm(cent)
                if norm > 0:
                    cent = cent / norm
                centroids[c] = cent

        # Nearest neighbours by cosine, computed in row chunks.
        #
        # The previous version materialised the whole n x n similarity matrix
        # (`vectors @ vectors.T`). At 20k papers that is 400M float64 = 3.2 GB,
        # which made /api/gaps for a single year take minutes and never return --
        # it looked like "the button does nothing". Only the top `k` neighbours
        # per row are ever read, so the matrix is never needed: each chunk costs
        # chunk x n, which is bounded regardless of corpus size.
        k = min(5, n - 1)
        top_neighbours = np.empty((n, max(1, k)), dtype=np.int64)
        for start in range(0, n, 2048):
            stop = min(start + 2048, n)
            block = vectors[start:stop] @ vectors.T
            for offset in range(block.shape[0]):
                block[offset, start + offset] = -np.inf
            part = np.argpartition(-block, max(1, k) - 1, axis=1)[:, :max(1, k)]
            top_neighbours[start:stop] = part

        big = [c for c, idx in members.items() if len(idx) >= sparse_size]

        # Resolve the bridge threshold. An absolute 0.6 never fires on real
        # TF-IDF/SVD geometry (measured max centroid cosine: 0.594), so derive a
        # data-relative threshold when the caller does not pin one.
        if bridge_sim is None:
            pair_sims = [
                float(centroids[a] @ centroids[b])
                for i_a, a in enumerate(big) for b in big[i_a + 1:]
                if a in centroids and b in centroids
            ]
            if pair_sims:
                arr = np.asarray(pair_sims, dtype=float)
                # "Close" = at or above the 75th percentile of inter-cluster
                # similarity, but never below a floor that would let unrelated
                # clusters in.
                resolved_bridge_sim = max(0.35, float(np.percentile(arr, 75)))
            else:
                resolved_bridge_sim = 0.6
        else:
            resolved_bridge_sim = float(bridge_sim)

        for i_a, a in enumerate(big):
            for b in big[i_a + 1:]:
                ca, cb = centroids.get(a), centroids.get(b)
                if ca is None or cb is None:
                    continue
                sim = float(ca @ cb)
                if sim < resolved_bridge_sim:
                    continue
                size_a, size_b = len(members[a]), len(members[b])
                # Measure under-linkage rather than a raw count of papers whose
                # neighbours span both clusters. The old rule ("bridge papers <=
                # min(size)//6") never fired on real data: with 8 clusters of
                # 25-111 papers there are always 11-39 spanning papers, which
                # exceeds the threshold even for genuinely well-connected fields.
                #
                # What actually characterises an under-combined pair is a
                # SHORTFALL of cross-links relative to what cluster sizes would
                # predict. Expected cross-links are estimated from cluster sizes
                # (chance of a random neighbour landing in the other cluster);
                # observed cross-links are counted from nearest neighbours.
                k_neigh = 5
                k_neigh = min(k_neigh, n - 1)
                observed = 0
                for p in members[a]:
                    top = top_neighbours[p][:k_neigh]
                    observed += sum(1 for t in top if int(labels[t]) == b)
                for p in members[b]:
                    top = top_neighbours[p][:k_neigh]
                    observed += sum(1 for t in top if int(labels[t]) == a)
                # Expected under a size-proportional null model.
                expected = (size_a * k_neigh * (size_b / max(1, n - 1))
                            + size_b * k_neigh * (size_a / max(1, n - 1)))
                if expected <= 0:
                    continue
                linkage = observed / expected
                # Under-combined = semantically close yet materially under-linked.
                if linkage > 0.5:
                    continue
                bridge_ids: list[str] = []
                for p in sorted(set(members[a]) | set(members[b])):
                    top = top_neighbours[p][:3]
                    neigh_clusters = {int(labels[t]) for t in top if t != p}
                    if a in neigh_clusters and b in neigh_clusters:
                        bridge_ids.append(ids[p])
                a_label = (top_terms.get(a) or ["area"])[0]
                b_label = (top_terms.get(b) or ["area"])[0]
                gaps.append({
                    "type": "bridge",
                    "title": f"{a_label} \u2194 {b_label} (under-combined)",
                    "explanation": (
                        f"Conceptually close (centroid cosine {sim:.2f}) but only "
                        f"{observed} cross-links vs {expected:.0f} expected "
                        f"({linkage:.0%} of expected)"
                    ),
                    "score": float(sim * (1.0 - linkage)),
                    "paper_ids": bridge_ids,
                    "papers": [meta[pid] for pid in bridge_ids],
                })

    # ---- sparse ---------------------------------------------------------- #
    for c, idx in members.items():
        size = len(idx)
        is_sparse = size <= sparse_size
        if not is_sparse and vectors is not None and size > 0:
            # Mean pairwise cosine distance from a SAMPLE of rows rather than the
            # full size x size matrix: a 6k-paper cluster is 36M float64 (288 MB)
            # and the answer only needs a stable mean. Deterministic stride keeps
            # the result reproducible for a given cluster.
            sub = np.asarray(vectors[idx], dtype=float)
            sample = sub[:: max(1, size // 256)][:256]
            if sample.shape[0] >= 2:
                sims = sample @ sub.T
                # Exclude self-similarity only where a sampled row is its own row.
                row_of = np.arange(sample.shape[0]) * max(1, size // 256)
                sims[np.arange(sample.shape[0]), row_of[: sample.shape[0]]] = np.nan
                mean_sim = float(np.nanmean(sims))
                if np.isfinite(mean_sim) and (1.0 - mean_sim) >= sparse_intra:
                    is_sparse = True
        if is_sparse:
            paper_ids = [ids[i] for i in idx][:12]
            c_label = (top_terms.get(c) or ["area"])[0]
            gaps.append({
                "type": "sparse",
                "title": f"Under-explored: {c_label}",
                "explanation": f"Only {size} papers / high internal spread",
                "score": 0.5,
                "paper_ids": paper_ids,
                "papers": [meta[pid] for pid in paper_ids],
            })

    # ---- cue ------------------------------------------------------------- #
    for i in range(n):
        matched = extract_gap_cues(abstracts[i] or "", max_len=240, limit=1)
        if matched:
            gaps.append({
                "type": "cue",
                "title": f"Stated gap in: {titles[i][:60]}",
                "explanation": matched[0],
                "score": 0.7,
                "paper_ids": [ids[i]],
                "papers": [meta[ids[i]]],
            })

    # Deterministic ordering: score desc, then type and title so two runs on the
    # same corpus always yield an identical list (stable UI + testable).
    gaps.sort(key=lambda g: (-g["score"], g["type"], g["title"]))
    return gaps[:40]
