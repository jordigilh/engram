"""Deterministic synthetic expressions of document text (zero LLM).

Produces a stable, reproducible summary layer per document — extractive key
sentences (TextRank: TF-IDF cosine graph + pagerank) plus TF-IDF keywords —
stored as retain metadata alongside raw chunks at ingest time. Same bytes in
always yield the same synthesis out (no model calls, no sampling, fixed
iteration), so banks carrying synthesis metadata are exactly reproducible
across rebuilds, unlike LLM-extracted facts.

Intended use: flow `process_doc_file` helpers compute one synthesis per
source file and merge ``key_sentences``/``keywords`` into each chunk's
retain metadata. Recall can boost or filter on them; nothing here changes
what is stored, only what accompanies it.
"""

import re

import networkx as nx
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

TOP_TERMS = 15
MIN_SENTENCES = 5
MAX_SENTENCES = 12
CHARS_PER_SENTENCE = 8000
SIMILARITY_THRESHOLD = 0.1


def split_sentences(text: str) -> list[str]:
    """Split markdown into candidate sentences.

    Headings, list items, and table rows stand alone; other lines split on
    sentence boundaries. Fenced code blocks are skipped (code is indexed via
    pgvector, not docs banks). Short fragments (<40 chars) are dropped.
    De-duplicated with order preserved.
    """
    lines: list[str] = []
    in_fence = False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not s:
            continue
        lines.append(s)
    out: list[str] = []
    for ln in lines:
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(`])", ln)
        out.extend(p.strip() for p in parts if len(p.strip()) > 40)
    seen: set[str] = set()
    uniq: list[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def synthesize_document(doc_id: str, text: str) -> dict:
    """Return ``{"doc_id", "key_sentences", "keywords", "stats"}`` for text.

    Sentence budget scales with document length (one per CHARS_PER_SENTENCE,
    clamped to [MIN_SENTENCES, MAX_SENTENCES]) and is returned in source
    order. Falls back to TF-IDF mass ranking when no sentence pair clears
    the similarity threshold (e.g. lists of unrelated one-liners).
    """
    sents = split_sentences(text)
    stats = {"n_sentences": len(sents), "source_chars": len(text)}
    if not sents:
        return {"doc_id": doc_id, "key_sentences": [],
                "keywords": [], "stats": {**stats, "coverage_chars": 0}}
    n_keep = min(MAX_SENTENCES, max(MIN_SENTENCES, len(text) // CHARS_PER_SENTENCE))
    vec = TfidfVectorizer(
        stop_words="english", token_pattern=r"[A-Za-z][A-Za-z0-9_.-]{2,}")
    try:
        X = vec.fit_transform(sents)
    except ValueError:  # empty vocabulary (no tokenizable words)
        keys = sents[:n_keep]
        return {"doc_id": doc_id, "key_sentences": keys, "keywords": [],
                "stats": {**stats, "coverage_chars": sum(len(s) for s in keys)}}
    sim = cosine_similarity(X)
    np.fill_diagonal(sim, 0.0)
    graph = nx.from_numpy_array(np.where(sim > SIMILARITY_THRESHOLD, sim, 0.0))
    if graph.number_of_edges() == 0:
        scores = X.sum(axis=1).A1
        ranked = sorted(range(len(sents)), key=lambda i: -scores[i])
    else:
        ranks = nx.pagerank(graph, weight="weight")
        ranked = sorted(range(len(sents)), key=lambda i: -ranks[i])
    keep = sorted(ranked[:n_keep])
    terms = vec.get_feature_names_out()
    col = X.sum(axis=0).A1
    top = sorted(range(len(terms)), key=lambda i: -col[i])[:TOP_TERMS]
    keys = [sents[i] for i in keep]
    return {"doc_id": doc_id, "key_sentences": keys,
            "keywords": [terms[i] for i in top],
            "stats": {**stats, "coverage_chars": sum(len(s) for s in keys)}}
