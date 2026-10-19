from __future__ import annotations

import numpy as np

from paper_ingest import semantics


def test_expand_aliases():
    assert "large language model" in semantics.expand_aliases("We train an llm on the data")
    # unrelated words are preserved and case is retained
    out = semantics.expand_aliases("The GAN and a cat sat")
    assert "generative adversarial network" in out
    assert "cat" in out
    assert "GAN" not in out  # replaced, lowercased canonical substituted


def test_expand_aliases_is_idempotent():
    """Regression: self-referential aliases used to stack canonical tokens on
    every pass, producing "diffusion model model model" in real cluster labels."""
    cases = [
        "attention mechanism",
        "diffusion model",
        "few shot learning",
        "we study a diffusion model for generation",
        "prompting with a capsule network",
        "knowledge distillation",
        "an llm with rag and rlhf",
        "contrastive language image pretraining",
    ]
    for text in cases:
        once = semantics.expand_aliases(text)
        twice = semantics.expand_aliases(once)
        assert once == twice, f"not idempotent for {text!r}: {once!r} -> {twice!r}"


def test_expand_aliases_no_duplicate_tokens():
    """Every alias key must expand without producing adjacent duplicate tokens."""
    for key in semantics.ALIASES:
        out = semantics.expand_aliases(key)
        tokens = out.split()
        for i in range(len(tokens) - 1):
            assert tokens[i] != tokens[i + 1], f"{key!r} -> {out!r} duplicates a token"


def test_expand_aliases_still_expands():
    """Idempotency must not be achieved by disabling expansion."""
    out = semantics.expand_aliases("an llm with rag")
    assert "large language model" in out
    assert "retrieval augmented generation" in out


def test_make_doc_weighting():
    doc = semantics.make_doc("Alpha", ["kw"], "Alpha is about alpha stuff")
    tokens = doc.split()
    title = [t for t in tokens if t.lower() == "alpha"]
    # title appears 3x, abstract once -> at least 3x as many title tokens
    assert len(title) >= 3
    # abstract 'stuff' appears once
    assert "stuff" in tokens
    # keywords appear twice
    assert len([t for t in tokens if t == "kw"]) == 2


def test_extract_rqs():
    abstract = (
        "We propose a new method for learning. "
        "How does this scale to large datasets? "
        "This sentence is irrelevant. "
        "We introduce a clever trick. "
    )
    rqs = semantics.extract_rqs(abstract)
    joined = " ".join(rqs)
    assert "How does this scale to large datasets?" in joined
    assert "This sentence is irrelevant." not in joined
    # deterministic & order-preserving
    assert semantics.extract_rqs(abstract) == rqs


def test_extract_rqs_prefers_open_questions_over_contributions():
    """On real abstracts the old cue list made 84% of papers look like they
    stated a research question, but they were contribution claims. A genuine
    open question must win, and a contribution-only abstract must not pretend
    to be an RQ when a real one exists."""
    abstract = (
        "We propose a new method for learning. "
        "We introduce a clever trick. "
        "It remains an open problem whether this scales. "
    )
    rqs = semantics.extract_rqs(abstract)
    assert len(rqs) == 1
    assert "open problem" in rqs[0]
    # the contribution claims must not be returned when a real RQ exists
    assert not any("clever trick" in r for r in rqs)


def test_extract_rqs_contribution_only_fallback_is_bounded():
    abstract = (
        "We propose a new method. "
        "We introduce a clever trick. "
        "We show that it works well on benchmarks."
    )
    rqs = semantics.extract_rqs(abstract)
    # No genuine open question: at most a couple of contribution fallbacks.
    assert len(rqs) <= 2
    # and never the "looks like a question" false positive
    assert all("irrelevant" not in r for r in rqs)


def test_label_clusters_from_tfidf():
    # Tiny corpus: two clusters with distinctive terms.
    docs = [
        "diffusion model image generation noise",  # cluster 0
        "diffusion model image generation sample",  # cluster 0
        "reinforcement learning policy reward",      # cluster 1
        "reinforcement learning agent value",        # cluster 1
    ]
    model = semantics.SemanticModel(dims=4)
    model.fit(docs)
    tfidf = model.vectorizer.transform(docs)
    labels = np.array([0, 0, 1, 1])
    out = semantics.label_clusters_from_tfidf(tfidf, labels, model.feature_names_, top_k=3)
    assert 0 in out and 1 in out
    assert " · " in out[0]
    assert out[0].strip() != ""
    assert out[1].strip() != ""


def test_detect_gaps_bridge_and_cue():
    # Two close clusters (bridge) plus one paper with an explicit gap cue.
    docs = [
        "large language model reasoning chain of thought",          # 0
        "large language model reasoning inference prompt",          # 0
        "graph neural network node embedding message passing",      # 1
        "graph neural network graph topology aggregation",          # 1
        "large language model graph neural network hybrid bridge",  # bridge (spans both)
        "Our approach works but remains an open problem for scaling.",  # cue
    ]
    model = semantics.SemanticModel(dims=4)
    model.fit(docs)
    vectors = model.transform(docs)
    labels = np.array([0, 0, 1, 1, 0, 0])
    abstracts = [
        "large language model reasoning.",
        "large language model reasoning inference.",
        "graph neural network embedding.",
        "graph neural network topology.",
        "a hybrid bridge paper.",
        "Our approach works but remains an open problem for scaling.",
    ]
    ids = [f"p{i}" for i in range(len(docs))]
    titles = [f"Paper {i}" for i in range(len(docs))]
    top_terms = {0: ["large language model"], 1: ["graph neural network"]}
    gaps = semantics.detect_gaps(vectors, labels, abstracts, ids, titles, top_terms)
    types = {g["type"] for g in gaps}
    assert "cue" in types
    cue = [g for g in gaps if g["type"] == "cue"][0]
    assert "open problem" in cue["explanation"]
    assert cue["paper_ids"] == ["p5"]


def test_detect_gaps_no_vectors_robust():
    labels = np.array([0, 0])
    abstracts = ["irrelevant text here", "future work is needed in this area"]
    ids = ["a", "b"]
    titles = ["A", "B"]
    top_terms = {0: ["area"], 1: ["area"]}
    gaps = semantics.detect_gaps(None, labels, abstracts, ids, titles, top_terms)
    assert any(g["type"] == "cue" for g in gaps)
    # no bridge/sparse should crash with None vectors
    assert all(g["type"] in ("cue", "sparse") for g in gaps)


def test_detect_gaps_bridge_with_large_clusters():
    # Two large (size >= sparse_size) clusters whose centroids are close in space:
    # the detector should flag them as an under-combined "bridge" gap. No single
    # paper lives in BOTH clusters, so the bridge paper set is empty (still flagged).
    n = 10
    v0 = np.array([1.0, 0.0])
    v1 = np.array([0.7, 0.714142842854285])  # norm 1, dot(v0,v1)=0.7 >= bridge_sim
    vectors = np.vstack([np.tile(v0, (n, 1)), np.tile(v1, (n, 1))])
    labels = np.array([0] * n + [1] * n)
    abstracts = ["a."] * (2 * n)
    ids = [f"p{i}" for i in range(2 * n)]
    titles = [f"Paper {i}" for i in range(2 * n)]
    top_terms = {0: ["concept a"], 1: ["concept b"]}
    gaps = semantics.detect_gaps(vectors, labels, abstracts, ids, titles, top_terms)
    assert any(g["type"] == "bridge" for g in gaps)
    bridge = [g for g in gaps if g["type"] == "bridge"][0]
    assert bridge["paper_ids"] == []
    assert 0.0 <= bridge["score"] <= 1.0


# --------------------------------------------------------------------------- #
# Regression tests for the real-corpus defects fixed by the Lead               #
# --------------------------------------------------------------------------- #
def test_semantic_model_out_dims_matches_transform_on_tiny_corpus():
    """Regression: `transform`'s zero fallback used `self.dims`, but `fit` clamps
    the component count on tiny corpora, so shapes disagreed and `v @ v.T` broke."""
    docs = ["alpha beta", "alpha gamma"]
    model = semantics.SemanticModel(dims=48)
    model.fit(docs)
    vectors = model.transform(docs)
    # Fitted width must equal the actual SVD component count, not the request.
    assert vectors.shape[1] == model.out_dims
    assert vectors.shape[1] < 48
    # Cosine math must work on the result.
    sims = vectors @ vectors.T
    assert sims.shape == (2, 2)


def test_semantic_model_transform_before_fit_has_consistent_width():
    model = semantics.SemanticModel(dims=8)
    vectors = model.transform(["anything"])
    assert vectors.shape[0] == 1
    assert vectors.shape[1] == model.out_dims


def test_semantic_model_fit_survives_degenerate_corpus():
    """min_df=2 used to raise "empty vocabulary" on tiny/degenerate corpora."""
    for corpus in (
        [""],
        ["", ""],
        ["the and of"],
        ["a"],
        ["", "transformer"],
    ):
        model = semantics.SemanticModel(dims=8).fit(corpus)
        # Must not raise; may legitimately stay unfitted.
        out = model.transform(corpus or [""])
        assert out.shape[0] == max(1, len(corpus))
        assert out.shape[1] == model.out_dims


def test_semantic_model_small_corpus_keeps_all_terms():
    """min_df must scale down so a 2-paper corpus is not reduced to nothing."""
    docs = [
        "diffusion models generate images",
        "graph networks embed molecules",
    ]
    model = semantics.SemanticModel(dims=4).fit(docs)
    assert model.vectorizer is not None
    assert model.feature_names_.shape[0] > 0


def test_label_cluster_rejects_filler_terms():
    """Regression: labels on real data came out as "aim · approximations · assuming"."""
    cluster = {
        "aim": 3.0, "assuming": 2.5, "approximations": 2.0,
        "diffusion model": 1.2, "graph neural network": 0.9,
    }
    global_mean = {"aim": 0.10, "assuming": 0.09, "approximations": 0.08,
                   "diffusion model": 0.30, "graph neural network": 0.25}
    label = semantics.label_cluster(cluster, global_mean, top_k=2, cluster_size=20)
    assert "aim" not in label
    assert "assuming" not in label
    assert "diffusion model" in label


def test_label_clusters_contains_topic_words():
    docs = [
        "diffusion model image generation noise",
        "diffusion model image generation sample",
        "graph neural network node embedding message passing",
        "graph neural network topology aggregation",
    ]
    model = semantics.SemanticModel(dims=4).fit(docs)
    tfidf = model.vectorizer.transform(docs)
    labels = np.array([0, 0, 1, 1])
    out = semantics.label_clusters_from_tfidf(tfidf, labels, model.feature_names_, top_k=3)
    assert "diffusion" in out[0]
    assert "graph" in out[1] or "neural" in out[1]


def test_extract_gap_cues_recalls_real_gap_phrasings():
    """Recall was ~5% on real abstracts; these are real gap statements."""
    real = [
        "Despite significant interest in TNPs, little attention has been given to incorporating symmetries.",
        "However, this setting has not been explored in prior work.",
        "This remains an important direction for future work.",
        "Existing methods fail to handle long-tail distributions.",
        "There is no known method that scales to this regime.",
    ]
    for sentence in real:
        cues = semantics.extract_gap_cues(sentence)
        assert cues, f"failed to recall gap sentence: {sentence!r}"


def test_extract_gap_cues_rejects_pure_background():
    """A blunt "lack of" must not fire on ordinary background/motivation."""
    background = (
        "Neural networks allow highly accurate predictions in many tasks, "
        "and their lack of robustness towards slight perturbations hampers deployment."
    )
    # Plain background describing a fact about the world is not a stated research gap.
    cues = semantics.extract_gap_cues(background)
    assert not cues


def test_detect_gaps_deterministic():
    n = 12
    rng = np.random.default_rng(0)
    vectors = rng.normal(size=(n, 8))
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    labels = np.array([0] * 6 + [1] * 6)
    abstracts = ["future work is needed in this area."] * n
    ids = [f"p{i}" for i in range(n)]
    titles = [f"Paper {i}" for i in range(n)]
    top_terms = {0: ["a"], 1: ["b"]}
    first = semantics.detect_gaps(vectors, labels, abstracts, ids, titles, top_terms)
    second = semantics.detect_gaps(vectors, labels, abstracts, ids, titles, top_terms)
    assert [g["title"] for g in first] == [g["title"] for g in second]
    assert len(first) <= 40


def test_detect_gaps_single_cluster_does_not_crash():
    n = 6
    vectors = np.eye(n)
    labels = np.zeros(n, dtype=int)
    abstracts = ["a sentence."] * n
    ids = [f"p{i}" for i in range(n)]
    titles = [f"Paper {i}" for i in range(n)]
    gaps = semantics.detect_gaps(vectors, labels, abstracts, ids, titles, {0: ["area"]})
    assert isinstance(gaps, list)
    assert not any(g["type"] == "bridge" for g in gaps)


def test_detect_gaps_bridge_detects_under_linked_pair():
    """Regression: bridge detection used to require an absolute centroid cosine
    of 0.6, which real TF-IDF/SVD geometry never reaches (measured max 0.594 over
    8 real clusters), so bridge gaps were ALWAYS empty. It also rejected pairs by
    a raw spanning-paper count that real data always exceeds. The rule is now
    "semantically close but under-linked relative to a size-proportional null".
    """
    n = 40
    rng = np.random.default_rng(7)
    # Two tight clusters that are close in cosine (centroids ~0.6 apart) but
    # whose papers link only within themselves (no cross-neighbourhood), i.e.
    # genuinely under-combined.
    base = np.zeros(16)
    base[0] = 1.0
    other = np.zeros(16)
    other[1] = 1.0
    other[0] = 0.6
    other[1] = 0.8
    half = n // 2
    vectors = np.vstack([
        np.tile(base, (half, 1)) + rng.normal(scale=0.001, size=(half, 16)),
        np.tile(other, (half, 1)) + rng.normal(scale=0.001, size=(half, 16)),
    ])
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    labels = np.array([0] * half + [1] * half)
    abstracts = ["a sentence."] * n
    ids = [f"p{i}" for i in range(n)]
    titles = [f"Paper {i}" for i in range(n)]
    top_terms = {0: ["concept a"], 1: ["concept b"]}
    gaps = semantics.detect_gaps(vectors, labels, abstracts, ids, titles, top_terms)
    bridges = [g for g in gaps if g["type"] == "bridge"]
    assert bridges, "under-linked but close cluster pair must be flagged as a bridge gap"
    b = bridges[0]
    assert "under-combined" in b["title"]
    assert "cross-links" in b["explanation"]

