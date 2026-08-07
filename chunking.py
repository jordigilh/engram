#!/usr/bin/env python3
"""Shared, content-stable chunking helpers for CocoIndex ingestion flows.

Used by cocoindex-flows.py, engram-cocoindex-flows.py, koku-cocoindex-flows.py,
and dcm-cocoindex-flows.py to turn a doc/issue's text into one or more
hindsight_retain() calls.

Why this exists (see docs/FINDINGS.md 2026-08-03): the original per-flow
`_split_text()` sliced content at fixed character offsets and named each
resulting document_id positionally (`--chunk{i}` / `-chunk{i}`). Any edit
inserted before the tail of a document shifts every downstream chunk
boundary by however many characters the edit added or removed, so
hindsight-api's delta retain correctly (and expensively) treats every one of
those repositioned chunks as "changed" even when the underlying text is
byte-identical to before. This was measured directly: prepending a single
~8KB dated entry to this repo's own reverse-chronological FINDINGS.md (the
normal way that file grows) cascaded ~100+ "no unchanged chunks, falling
back to full retain" events for chunks whose text hadn't meaningfully
changed, each one paying a real extraction + downstream consolidation pass.
Similarly, every kubernaut-issues PR/issue that gets a label or state change
(routine — happens to nearly every PR on merge) rewrites the volatile
"State: ... | Labels: ..." header line that used to sit inside the chunked
text, cascading every comment chunk after it even though no comment text
changed.

The helpers below key each logical unit by *stable* identifiers (a
section's own heading text, a comment's ordinal position) instead of raw
character position, so an edit/insertion in one place no longer reindexes
-- and therefore no longer invalidates the stored content-hash of -- every
other unaffected chunk. This mirrors the same fix already applied to
transcript learning-window ids (see cocoindex-flows.py's
`_window_document_id`): a stable, content-derived suffix that only changes
when the content it names actually changes.

Callers assemble the final document_id as `base_id` (key == "") or
`f"{base_id}{sep}{key}"` (key != ""), where `sep` is whatever the call site
already uses ("--" for docs, "-" for issues) -- kept caller-side so each
flow's existing document_id convention (and therefore its existing,
already-ingested chunk-0 rows) is undisturbed.
"""
from __future__ import annotations

import asyncio
import hashlib
import pathlib
import re

from cocoindex.ops.sentence_transformers import SentenceTransformerEmbedder
from cocoindex.ops.text import RecursiveSplitter, detect_code_language

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+.*$", re.MULTILINE)
_code_splitter = RecursiveSplitter()
_code_embedder = SentenceTransformerEmbedder("sentence-transformers/all-MiniLM-L6-v2")


def split_fixed_window(text: str, chunk_size: int = 800, chunk_overlap: int = 200) -> list[str]:
    """Sliding-window character split, breaking at newlines when possible.

    This is the original `_split_text()` implementation, relocated here
    unchanged. Still used directly for content with no stable internal
    anchors (e.g. code, or markdown with no headings at all), and as a
    fallback for sub-splitting a single already-stable section/comment that
    is itself too large for one retain call. Must NOT be used to chunk a
    whole multi-section document end to end -- see module docstring for why.
    """
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end < len(text):
            break_point = text.rfind("\n", start, end)
            if break_point > start + chunk_size // 2:
                end = break_point + 1
        chunks.append(text[start:end])
        start = end - chunk_overlap
    return chunks


def split_code(text: str, filename: str, chunk_size: int = 1000, chunk_overlap: int = 300) -> list[str]:
    """AST-aware code split, via cocoindex's own tree-sitter-backed
    RecursiveSplitter, using the language cocoindex detects from
    `filename`'s extension.

    Why this exists (see docs/FINDINGS.md 2026-08-07): code_app's
    process_code_file() used to call split_fixed_window() directly on
    source code -- a plain character-offset slice that can (and did) cut a
    function body in half mid-declaration. docs/COCOINDEX.md and this
    project's README have described code chunking as tree-sitter/AST-aware
    since inception; the cocoindex *framework* has in fact supported this
    the whole time (detect_code_language/RecursiveSplitter), engram's own
    flow scripts just never called into it -- a docs-vs-implementation
    drift, not a missing upstream capability. RecursiveSplitter descends
    the language's AST (function/class/block nodes) before ever falling
    back to a raw character cut, and only takes that fallback for a single
    node that still exceeds chunk_size on its own.

    An unrecognized extension (detect_code_language() returns None) is not
    an error: RecursiveSplitter still does generic paragraph/line-aware
    splitting without a language grammar, which is strictly no worse than
    the old fixed-window cut, so no special-casing is needed here.

    Return contract matches split_fixed_window() exactly (a plain
    list[str], no keys) -- code_app has no stable-heading/ordinal concept
    and isn't routed through hindsight-api's delta-retain/consolidation
    pipeline, so a positional list is fine here unlike the markdown/issue
    splitters above.
    """
    language = detect_code_language(filename=filename)
    chunks = _code_splitter.split(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap, language=language)
    return [chunk.text for chunk in chunks]


async def embed_code_chunks(chunks: list[str]) -> list[list[float]]:
    """Embed a file's code chunks via cocoindex's own batching embedder,
    replacing each *-cocoindex-flows.py script's separate
    _embedder/_get_embedder()/_embed_text() globals that called a bare
    SentenceTransformer.encode() once per chunk, sequentially.

    Why this exists (see docs/FINDINGS.md 2026-08-07): today's tree-sitter
    chunking backfill logged every one of ~25,000 code chunks going through
    model.encode() one at a time. SentenceTransformerEmbedder.embed() is
    decorated `@coco.fn.as_async(batching=True, max_batch_size=64)`, which
    coalesces *concurrent* calls into one batched model.encode(list) call
    and halves-and-retries the batch on accelerator OOM
    (coco.RetryWithSmallerBatch) -- neither of which a for-loop of awaited
    single-text calls can ever trigger. asyncio.gather() below is what
    actually produces the concurrency the batcher coalesces; awaiting each
    embed() one at a time in a loop would still work but would silently
    never batch.
    """
    if not chunks:
        return []
    vectors = await asyncio.gather(*(_code_embedder.embed(c) for c in chunks))
    return [v.tolist() for v in vectors]


async def code_embedding_dim() -> int:
    """The active code embedder's output dimension, for the pgvector
    column's vector(N) type -- derived from the model itself instead of
    each *-cocoindex-flows.py script hardcoding vector(384) independently
    of whichever model _code_embedder actually loads."""
    return await _code_embedder.dimension()


def find_code_files(
    root: pathlib.Path,
    included_patterns: list[str],
    excluded_patterns: list[str] | None = None,
) -> list[pathlib.Path]:
    """Enumerate files under `root` matching `included_patterns` (gitignore-
    style globs, `**` supported) and none of `excluded_patterns`.

    Backs the `*_code_pattern_search` MCP tools (see *-cocoindex-search.py,
    docs/FINDINGS.md 2026-08-07 CodePattern spike): CodePattern.match_file()
    needs a concrete file list to scan, and this reuses the exact same
    included/excluded glob lists each *-cocoindex-flows.py already passes to
    localfs.walk_dir(path_matcher=PatternFilePathMatcher(...)) for ingestion,
    so pattern search never drifts out of sync with what's actually indexed
    (vendor/, generated code, node_modules, etc. stay excluded from both).

    A missing root returns an empty list rather than raising -- a
    project whose live checkout isn't present locally (e.g. running from a
    machine that only has some of kubernaut/koku/dcm/engram cloned) should
    degrade to "no matches" for that one root, not crash the whole query.
    """
    if not root.is_dir():
        return []
    excluded_patterns = excluded_patterns or []
    found: set[pathlib.Path] = set()
    for pattern in included_patterns:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(rel.full_match(pat) for pat in excluded_patterns):
                continue
            found.add(path)
    return sorted(found)


def _stable_key(text: str) -> str:
    """Short, stable digest for use as a document_id suffix. Same algorithm
    (sha1, first 16 hex chars) as _window_document_id() for consistency."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _numbered_fixed_window(text: str, chunk_size: int, chunk_overlap: int) -> list[tuple[str, str]]:
    """Fallback numbering matching the pre-existing positional convention
    (bare key for the first chunk, "chunk{i}" for i>=1) -- used verbatim
    when there is no stable anchor to key off of."""
    chunks = split_fixed_window(text, chunk_size, chunk_overlap)
    return [("" if i == 0 else f"chunk{i}", c) for i, c in enumerate(chunks)]


def split_markdown_sections(
    content: str, chunk_size: int = 800, chunk_overlap: int = 200,
) -> list[tuple[str, str]]:
    """Split markdown content into (key, text) pairs anchored at heading
    boundaries instead of raw character offsets.

    - Content before the first heading (or the whole document, if it has no
      headings at all) gets key "" -- matching the pre-existing convention
      where the first/only chunk carries the bare document_id, no suffix.
    - Every heading-anchored section after that gets a key derived from a
      hash of its own heading line, so inserting a brand-new section (e.g.
      prepending a new dated entry to a changelog-style doc) does not
      change any existing section's key, content, or stored content-hash.
    - A section that individually exceeds chunk_size is further split with
      split_fixed_window(), suffixed "-part{n}" (n>=1) scoped to that
      section only -- so the fallback's positional cascade is bounded to
      just that one section instead of the whole document.
    """
    matches = list(_HEADING_RE.finditer(content))
    if not matches:
        return _numbered_fixed_window(content, chunk_size, chunk_overlap)

    sections: list[tuple[str, str]] = []
    preamble = content[: matches[0].start()]
    if preamble.strip():
        sections.append(("", preamble))

    for idx, m in enumerate(matches):
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
        body = content[start:end]
        key = "" if not sections else _stable_key(m.group(0))
        sections.append((key, body))

    result: list[tuple[str, str]] = []
    for key, body in sections:
        if len(body) <= chunk_size:
            result.append((key, body))
            continue
        for part_idx, part in enumerate(split_fixed_window(body, chunk_size, chunk_overlap)):
            if part_idx == 0:
                part_key = key
            else:
                part_key = f"{key}-part{part_idx}" if key else f"part{part_idx}"
            result.append((part_key, part))
    return result


def split_issue_sections(
    header: str, comments: list[str], chunk_size: int = 1200, chunk_overlap: int = 300,
) -> list[tuple[str, str]]:
    """Split issue/PR/ticket content into (key, text) pairs: one section for
    the static header + description, then one section per comment, keyed by
    the comment's own ordinal position in the already-filtered `comments`
    list -- not by character offset into a concatenated string.

    Comments are append-only in both GitHub's and Jira's APIs, so a new
    comment only ever adds a new key at the end; it never changes the key
    or stored content-hash of any earlier comment. Callers must exclude
    volatile fields (state, labels, status) from `header` -- see module
    docstring -- otherwise every such change still cascades the header's
    own chunk (bounded to header only, never the comments, with this
    split).
    """
    result: list[tuple[str, str]] = []
    if len(header) <= chunk_size:
        result.append(("", header))
    else:
        for part_idx, part in enumerate(split_fixed_window(header, chunk_size, chunk_overlap)):
            result.append(("" if part_idx == 0 else f"part{part_idx}", part))

    for c_idx, comment_text in enumerate(comments):
        key = f"comment{c_idx}"
        if len(comment_text) <= chunk_size:
            result.append((key, comment_text))
        else:
            for part_idx, part in enumerate(split_fixed_window(comment_text, chunk_size, chunk_overlap)):
                result.append((key if part_idx == 0 else f"{key}-part{part_idx}", part))
    return result
