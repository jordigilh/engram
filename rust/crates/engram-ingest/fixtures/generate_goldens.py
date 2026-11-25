#!/usr/bin/env python3
"""Generate the frozen parity fixtures for the Rust `engram-ingest` crate.

This is developer tooling, not a test: pytest collects only `tests/`, and this
file lives under `rust/`. Run it from the repository root with the deployed flow
interpreter (Python 3.14, the version the live `engram-flows-configured` venv
uses):

    PYTHONPATH=src ~/.engram/venv/bin/python \
        rust/crates/engram-ingest/fixtures/generate_goldens.py

It imports the real Python implementations, captures `_retain_document` calls
by replacing the module-level `hindsight_retain`, and asserts that its own
sklearn/networkx recomputation reproduces `synthesize_document`. The emitted
intermediates (TF-IDF matrix, row sums, ranks) let the Rust tests prove the
maths, not only the final selection.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import platform
import subprocess

import networkx as nx
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

import engram.chunking as chunking
import engram.flows.configured as configured
import engram.synthesis as synthesis

ROOT = pathlib.Path(__file__).resolve().parents[4]
FIXTURES = pathlib.Path(__file__).resolve().parent
TOKEN_PATTERN = r"[A-Za-z][A-Za-z0-9_.-]{2,}"


def sha256_file(relative: str) -> str:
    return hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def base_id_cases() -> list[dict]:
    cases = [
        ("engram", "a/b.md"),
        ("repo", "docs/x.tar.gz"),
        ("repo", "a."),
        ("repo", "a.."),
        ("repo", ".bashrc"),
        ("repo", "dir/.hidden.md"),
        ("repo", "noext"),
        ("repo", "/a/b.md"),
        ("repo", "dir.with.dots/file"),
        ("repo", "a b.md"),
        ("repo", "docs/README"),
    ]
    return [
        {
            "source_tag": tag,
            "relative": relative,
            "expected": configured._document_base_id(tag, relative),
        }
        for tag, relative in cases
    ]


def chunking_cases() -> list[dict]:
    cases = [
        ("plain_short", "just some plain text", 800, 200),
        ("plain_long", "line\n" * 500, 800, 200),
        ("empty", "", 800, 200),
        ("whitespace", "  \n\t", 800, 200),
        ("single_heading", "# Hello\n\nSome content", 800, 200),
        (
            "preamble_then_headings",
            "Intro\n\n# Title\n\nBody\n\n## A\n\nBody A\n\n### B\n\nBody B\n",
            800,
            200,
        ),
        ("no_preamble_headings", "# Title\n\nIntro\n\n## A\n\nBody A\n", 800, 200),
        ("oversized_first_section", "## Big\n\n" + "x\n" * 500, 800, 200),
        (
            "oversized_later_section",
            "Preamble long enough to stand alone.\n\n## Big\n\n" + "x\n" * 500,
            800,
            200,
        ),
        ("heading_levels", "# One\n\na\n\n## Two\n\nb\n\n### Three\n\nc\n\n#### Four\n\nd\n\n##### Five\n\ne\n\n###### Six\n\nf\n", 800, 200),
        ("invalid_headings", "#no\n\n####### seven\n\n#\tTabbed\n\nbody\n", 800, 200),
        ("unicode", "# Héad\n\n" + "é" * 900, 800, 200),
        ("crlf", "# Title\r\n\r\nBody line one.\r\n\r\n## Section\r\n\r\nBody two.\r\n", 800, 200),
        (
            "all_heading_levels_oversized",
            "# A\n\n" + "a" * 900 + "\n\n## B\n\n" + "b" * 900,
            800,
            200,
        ),
    ]
    return [
        {
            "name": name,
            "content": content,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "expected": [
                [key, text]
                for key, text in chunking.split_markdown_sections(
                    content, chunk_size=chunk_size, chunk_overlap=chunk_overlap
                )
            ],
        }
        for name, content, chunk_size, chunk_overlap in cases
    ]


def document_cases() -> list[dict]:
    cases = [
        (
            "markdown_basic",
            "repo",
            "docs/guide.md",
            "# Title\n\nIntro paragraph that is long enough to matter here.\n\n## Section\n\nBody body body body body body body body.\n",
            "2026-11-24T10:15:30.123456",
        ),
        (
            "preamble",
            "repo",
            "README.md",
            "Preamble text for the root document.\n\n# Heading\n\nMore text that is long enough.\n",
            None,
        ),
        (
            "duplicate_headings",
            "repo",
            "docs/duplicate.md",
            "Preamble long enough for a section.\n\n## Notes\n\nFirst notes body.\n\n## Notes\n\nSecond notes body.",
            None,
        ),
        (
            "unicode",
            "engram",
            "docs/über.md",
            "# Überschrift\n\nEin ausreichend langer Satz für die Synthese.\n\n## Abschnitt\n\nNoch mehr Text für den Abschnitt.\n",
            "2026-11-24T10:15:30",
        ),
        # Whitespace-only files are covered by the Rust unit test for the
        # `process_doc_file` early return; `_retain_document` itself does not
        # skip them.
    ]
    result = []
    for name, tag, relative, content, timestamp in cases:
        captured: list[dict] = []

        def capture(**kwargs):
            captured.append(kwargs)

        configured.hindsight_retain = capture
        configured._retain_document(
            bank_id="fixture-bank",
            content=content,
            source_tag=tag,
            relative=relative,
            timestamp=timestamp,
        )
        result.append(
            {
                "name": name,
                "source_tag": tag,
                "relative": relative,
                "content": content,
                "timestamp": timestamp,
                "expected": [
                    {
                        "document_id": call["document_id"],
                        "content": call["content"],
                        "metadata": call["metadata"],
                        "tags": call["tags"],
                        "timestamp": call["timestamp"],
                    }
                    for call in captured
                ],
            }
        )
    return result


def analysis_for(text: str) -> dict:
    sentences = synthesis.split_sentences(text)
    n_keep = min(
        synthesis.MAX_SENTENCES,
        max(synthesis.MIN_SENTENCES, len(text) // synthesis.CHARS_PER_SENTENCE),
    )
    result = {
        "sentences": sentences,
        "n_keep": n_keep,
        "vocabulary": [],
        "tfidf": [],
        "row_sums": [],
        "ranks": [],
        "keyword_scores": [],
        "key_indices": [],
        "keyword_indices": [],
        "empty_vocabulary": False,
    }
    if not sentences:
        result["empty_vocabulary"] = True
        return result

    vectorizer = TfidfVectorizer(
        stop_words="english", token_pattern=TOKEN_PATTERN
    )
    try:
        matrix = vectorizer.fit_transform(sentences)
    except ValueError:
        result["empty_vocabulary"] = True
        result["key_indices"] = list(range(min(n_keep, len(sentences))))
        return result

    similarity = cosine_similarity(matrix)
    np.fill_diagonal(similarity, 0.0)
    graph = nx.from_numpy_array(
        np.where(similarity > synthesis.SIMILARITY_THRESHOLD, similarity, 0.0)
    )
    row_sums = matrix.sum(axis=1).A1
    if graph.number_of_edges() == 0:
        scores = row_sums
        ranked = sorted(range(len(sentences)), key=lambda index: -scores[index])
        ranks = row_sums.tolist()
    else:
        values = nx.pagerank(graph, weight="weight")
        ranks = [values[index] for index in range(len(sentences))]
        ranked = sorted(range(len(sentences)), key=lambda index: -ranks[index])
    keep = sorted(ranked[:n_keep])

    terms = vectorizer.get_feature_names_out()
    column_sums = matrix.sum(axis=0).A1
    top = sorted(range(len(terms)), key=lambda index: -column_sums[index])[
        : synthesis.TOP_TERMS
    ]

    expected = synthesis.synthesize_document("fixture", text)
    if expected["key_sentences"] != [sentences[index] for index in keep]:
        raise RuntimeError("sentence selection diverged from synthesize_document")
    if expected["keywords"] != [terms[index] for index in top]:
        raise RuntimeError("keyword selection diverged from synthesize_document")

    result.update(
        {
            "vocabulary": [str(term) for term in terms],
            "tfidf": matrix.toarray().tolist(),
            "row_sums": [float(value) for value in row_sums],
            "ranks": [float(value) for value in ranks],
            "keyword_scores": [float(value) for value in column_sums],
            "key_indices": keep,
            "keyword_indices": top,
        }
    )
    return result


def synthesis_cases() -> list[dict]:
    related = (
        "The ingestion ledger records every document hash in Postgres so the "
        "worker can reconcile changed files after a restart.\n"
        "A document hash lets the ledger skip unchanged files without reading "
        "their content again.\n"
        "When a document hash changes the worker retains every chunk of the "
        "file with fresh synthesis metadata.\n"
        "The ledger stores chunk hashes so a removed section can be deleted "
        "and tombstoned after the edit.\n"
        "Postgres leases stop two workers from processing the same ingestion "
        "job at the same time.\n"
        "A retry uses the same stable document id so Hindsight can deduplicate "
        "the retained content.\n"
    )
    unrelated = (
        "Zebra quantum lanterns orbit the silent cathedral of numbers.\n"
        "Molten bicycles whisper riddles to the patient copper engine.\n"
        "Velvet glaciers negotiate treaties with invisible marigold pilots.\n"
        "A porcelain thunderstorm interviews seventeen retired compasses.\n"
        "Glass parrots memorize the geometry of abandoned lighthouses.\n"
        "Tangerine algebra dreams beneath the cautious iron meadow.\n"
    )
    empty_vocabulary = (
        "The and of to in on at by for with from.\n"
        "It is the one that they have been with from the start.\n"
        "This and that are of the same kind and not of another.\n"
        "We were there when they had been with us for the whole time.\n"
        "You are the one who can be with them at the same time.\n"
        "And so it is with all of the things that are in the end.\n"
    )
    tie = (
        "Alpha bravo charlie delta echo foxtrot golf hotel india juliet.\n"
        "Kilo lima mike november oscar papa quebec romeo sierra tango.\n"
        "Uniform victor whiskey xray yankee zulu amber bronze copper.\n"
        "Diamond emerald flint granite hazel indigo jasper krypton lava.\n"
        "Magenta nickel onyx pearl quartz ruby silver topaz umber.\n"
        "Violet walnut xenon yarrow zinc adobe basalt cobalt dune.\n"
    )
    long_sentences = "\n".join(
        f"Sentence number {index} "
        + "alpha beta gamma delta epsilon zeta eta theta "
        + f"word{index} " * 900
        + "."
        for index in range(20)
    )
    cases = {
        "related_documents": related,
        "unrelated_one_liners": unrelated,
        "empty_vocabulary": empty_vocabulary,
        "tied_scores": tie,
        "short_document": "Only one sentence here that is long enough to survive the filter.",
        "long_document_cap": long_sentences,
        "fenced_code_skipped": (
            "```\nThis fenced sentence is long enough to be considered but must be skipped.\n```\n"
            "A visible sentence that is long enough to be selected by synthesis.\n"
            "Another visible sentence that is long enough to be selected too.\n"
        ),
    }
    result = []
    for name, text in cases.items():
        expected = synthesis.synthesize_document("fixture", text)
        result.append(
            {
                "name": name,
                "doc_id": expected["doc_id"],
                "text": text,
                "expected": {
                    "key_sentences": expected["key_sentences"],
                    "keywords": expected["keywords"],
                    "stats": expected["stats"],
                    "analysis": analysis_for(text),
                },
            }
        )
    return result


def main() -> None:
    payload = {
        "provenance": {
            "python": platform.python_version(),
            "sklearn": __import__("sklearn").__version__,
            "networkx": nx.__version__,
            "source_commit": git_commit(),
            "sources": {
                "src/engram/chunking.py": sha256_file("src/engram/chunking.py"),
                "src/engram/synthesis.py": sha256_file("src/engram/synthesis.py"),
                "src/engram/flows/configured.py": sha256_file(
                    "src/engram/flows/configured.py"
                ),
            },
        },
        "base_ids": base_id_cases(),
        "chunking": chunking_cases(),
        "documents": document_cases(),
        "synthesis": synthesis_cases(),
    }
    (FIXTURES / "goldens.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
