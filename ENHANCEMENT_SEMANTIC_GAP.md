# Enhancement: strengthen graph semantics & idea-finding (RQ/RG)

Goal (from user): strengthen the paper graph's **semantics** and **discriminatory/resolution power**
(图谱语义与分辨能力) at the **algorithm level**, so a user brainstorming an idea can (1) quickly
retrieve the right paper **abstracts** and (2) surface **new possibilities** — Research Questions
(**RQ**) and Research Gaps (**RG**) — to inspire ideas.

Design stays **CPU-only**, within the existing dependencies (`numpy`, `scikit-learn`), with **no
large model downloads**. Everything is local; no git commit/push.

## 1. New module `research-self/paper_ingest/semantics.py`

Pure, deterministic, unit-testable functions + a small pickle-backed model store.

### 1.1 Vocabulary bridging (alias expansion) — increases resolution
`ALIASES: dict[str, str]` curated academic surface-forms → canonical concept, e.g.
`"llm"→"large language model"`, `"gan"→"generative adversarial network"`,
`"rl"→"reinforcement learning"`, `"ssl"→"self supervised learning"`, `"vit"→"vision transformer"`,
`"diffusion"→"diffusion model"`, `"nlp"→"natural language processing"`, `"cv"→"computer vision"`,
`"gnn"→"graph neural network"`, `"transformer"→"transformer"`, etc. (≈40 entries).
`expand_aliases(text: str) -> str`: token-level replace, word-boundary safe, lowercase-invariant.

### 1.2 Weighted document construction
`make_doc(title, keywords, abstract) -> str`:
`(title + " ") * 3 + (" ".join(keywords) + " ") * 2 + (abstract or "")`.
Applied to every paper before vectorizing. (Currently title+abstract are equal weight → this
sharpens topic resolution.)

### 1.3 Richer TF-IDF + SVD (replaces inline code in `generate_graph`)
`SemanticModel` wraps `TfidfVectorizer` + `TruncatedSVD`:
- `TfidfVectorizer(max_features=1024, stop_words="english", ngram_range=(1,2),
  sublinear_tf=True, min_df=2, max_df=0.5, strip_accents="unicode")`
- `TruncatedSVD(n_components=dims, random_state=17)` where
  `dims = min(48, n-1, max(2, n_features-1))`.
- `fit(corpus: list[str]) -> self`, `transform(texts) -> np.ndarray` (l2-normalized rows).
- `save(path)`, `load(path)` via `joblib` (graceful on failure → returns None).

### 1.4 Meaningful topic labels (replaces "Topic N")
`label_cluster(term_weights_in_cluster, global_term_weights, top_k=3) -> str`:
pick terms with highest **within-cluster / global** TF-IDF ratio, join with " · ".
Used to overwrite `graph_topics.label` for both `cluster` and `region` levels, so the map becomes
human-readable (e.g. "diffusion model · image generation").

### 1.5 Research-question (RQ) extraction
`extract_rqs(abstract: str) -> list[str]`:
- split into sentences;
- return sentences matching cues: contains "?", or starts-with/contains
  `we propose|we present|we introduce|we develop|we aim|our goal|our contribution|
   this paper (proposes|introduces|presents|aims|studies|investigates)|we investigate|
   we show that|we study`; trimmed to ≤ 240 chars.
Deterministic, no deps.

### 1.6 Research-gap (RG) detection → `detect_gaps(...)`
Inputs: paper vectors (normalized SVD), cluster labels, abstracts, centroids.
Returns gap candidates, each `{type, title, explanation, score, paper_ids, papers:[{id,title,abstract}]}`:
- **bridge** (concept co-occurrence gap): for every pair of populated clusters (A,B),
  `sim = cosine(centroid_A, centroid_B)`; bridge papers = papers whose top neighbours span both
  clusters. If `sim` high (≥0.6) **and** bridge-paper count low (≤ some threshold relative to
  cluster sizes) → "A ↔ B are conceptually close but rarely combined" (candidate RG).
- **sparse** (under-explored region): cluster with small size but large mean intra-distance →
  "under-explored area".
- **cue** (explicit gap statements): scan abstracts for gap phrases
  `remains (an )?open|open (problem|question)|under[- ]explored|understudied|
   little (attention|work|research)|lack(s)? of|limited (understanding|study)|
   not well (understood|studied)|few studies|future work|remains challenging`;
  return flagged papers with the matched sentence as `explanation`.

## 2. Modify `service.py::generate_graph()`
- Build docs with `make_doc` + `expand_aliases`.
- Fit `SemanticModel`; store vectors; keep t-SNE layout (perplexity rule unchanged); KMeans labels.
- Compute and store **meaningful** `graph_topics.label` via `label_cluster`.
- Persist the fitted `SemanticModel` to `Data/graph/<venue>_<year>_semantic.pkl` (best-effort).
- Keep backward-compatible output (`x,y,region,cluster,vectors,...`).

## 3. New/modified API in `api.py`
- `POST /api/papers/semantic` body `{query, venue?, year?, limit?=10}`:
  load model for (venue,year) (default all→fallback to first available), encode query, return
  top-K by cosine: `{items:[{id,slug,title,abstract,authors,venues,year,score}], method, note?}`.
  If no model: fall back to FTS keyword search, set `note:"keyword fallback"`.
- `GET /api/gaps?venue=&year=`: `detect_gaps(...)` →
  `{gaps:[...], topics:[{number,label,size,top_terms}]}`.
- `GET /api/papers/{id}`: add `rqs` (from `extract_rqs`) and `gap_cues` (matched sentences) to payload.

## 4. Frontend (`acceptodds`) — new "Idea Finder"
- New route `src/app/ideas/page.tsx` (server component) + client component
  `src/components/IdeaFinder.tsx`:
  - **Semantic abstract search**: textarea (an idea / RQ) → `POST /api/papers/semantic` →
    list of cards: title (link), score, abstract snippet, venue/year.
  - **Research gaps panel**: `GET /api/gaps` → gap cards grouped by type
    (bridge / sparse / cue), each expandable to example paper abstracts (links).
- `src/app/papers/[slug]/page.tsx`: render `rqs` and `gap_cues` sections (from payload).
- Add small typed helpers in `src/lib/paper-types.ts` (SemanticResult, Gap, etc.).
- Constraint (AGENTS.md): preserve MapCanvas/layout; run `npm test`, `npm run typecheck`,
  `npm run build`; format with Prettier. No trading/auth.

## 5. Tests
- `research-self/tests/test_semantics.py`: alias expansion, make_doc weighting, extract_rqs,
  label_cluster, detect_gaps on a tiny synthetic corpus (no DB). Keep deterministic (fixed seeds).
- Frontend: a minimal vitest for `extract_rqs`/`label_cluster` if ported; otherwise typecheck covers.

## 6. Do NOT
- Do not `git commit` / `git push`. Do not add heavy ML deps or download models.
- Do not change the `graph_points`/`graph_topics` DB schema (reuse columns; pack top_terms into label).
