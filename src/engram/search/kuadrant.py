#!/usr/bin/env python3
r"""Kuadrant code search MCP server.

Provides hybrid code search (dense vectors + BM25) over the
cocoindex.kuadrant_code_embeddings table (Go + Rust source across 7 of the
8 onboarded Kuadrant repos -- `architecture` has no source code). Results
are fused using Reciprocal Rank Fusion (RRF) so both semantic similarity
and exact keyword matches contribute to ranking.

Only `kuadrant_code_search` is exposed through the shared, recall-only
`kuadrant` gateway mount wired into every praxis-* repo (see
engram_gateway.py's RELEVANT_TOOLS_BY_BACKEND["kuadrant_code"]) -- the
pattern-search and call-graph tools below exist for direct CLI/local use
only, same as every other onboarded org's search module, but aren't part
of the cross-mounted surface: Kuadrant is ingested for reference recall,
not for active refactoring-impact analysis.

Usage:
    python3 kuadrant-cocoindex-search.py                    # Start MCP server (stdio)
    python3 kuadrant-cocoindex-search.py --query "how does limitador rate limit requests"
    python3 kuadrant-cocoindex-search.py --query "AuthConfig" --mode dense
    python3 kuadrant-cocoindex-search.py --pattern 'func \NAME(\(A*\))' --language go
    python3 kuadrant-cocoindex-search.py --blast-radius Enforce --depth 2 --language go
"""

import argparse
import logging
import os
import pathlib
import sys
from typing import Any

# This file is part of the engram.search package (src/engram/search/).
# sys.path[0] for a script invoked via a symlink (as launchd does) resolves
# to the symlink's realpath target directory (src/engram/search/), not the
# symlink's own directory -- src/ itself must still be added explicitly so
# `engram` resolves as a top-level package rather than needing this file to
# be run via `-m`/an installed console script (not yet true in this repo).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))
from engram import callgraph, chunking  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("kuadrant-cocoindex-search")

PG_URL = os.environ.get(
    "COCOINDEX_PG_URL",
    "postgresql://hindsight:hindsight@localhost:5432/hindsight",
)
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
RRF_K = 60

# Same env var (and default) as kuadrant-cocoindex-flows.py, so pattern
# search walks the exact same checkouts the ingestion flow indexes.
KUADRANT_ORG_DIR = pathlib.Path(os.environ.get(
    "KUADRANT_ORG_DIR", os.path.expanduser("~/go/src/github.com/Kuadrant"),
))

# (repo_tag, root, included_patterns, excluded_patterns, language) -- mirrors
# kuadrant-cocoindex-flows.py's KUADRANT_REPOS/_CODE_PATTERNS exactly.
# `architecture` (no source) is excluded, same as code_main.
_REPO_LANGUAGES: dict[str, str] = {
    "kuadrant-operator": "go",
    "limitador": "rust",
    "wasm-shim": "rust",
    "authorino": "go",
    "dns-operator": "go",
    "authorino-operator": "go",
    "limitador-operator": "go",
}
_LANGUAGE_PATTERNS: dict[str, tuple[list[str], list[str]]] = {
    "go": (["**/*.go"], ["**/vendor/**", "**/*_test.go", "**/zz_generated*"]),
    "rust": (["**/*.rs"], ["**/target/**"]),
}
_PATTERN_SEARCH_ROOTS = [
    (tag, KUADRANT_ORG_DIR / tag, *_LANGUAGE_PATTERNS[language])
    for tag, language in _REPO_LANGUAGES.items()
]

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(EMBEDDING_MODEL)
        log.info("Loaded embedding model: %s", EMBEDDING_MODEL)
    return _model


def _embed_query(query: str) -> list[float]:
    model = _get_model()
    return model.encode(query, normalize_embeddings=True).tolist()


def _rrf_fuse(
    dense_results: list[dict],
    bm25_results: list[dict],
    limit: int,
) -> list[dict]:
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}

    for rank, r in enumerate(dense_results):
        key = r["id"]
        scores[key] = scores.get(key, 0) + 1 / (RRF_K + rank + 1)
        if key not in items:
            items[key] = r

    for rank, r in enumerate(bm25_results):
        key = r["id"]
        scores[key] = scores.get(key, 0) + 1 / (RRF_K + rank + 1)
        if key not in items:
            items[key] = r

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
    return [{**items[key], "rrf_score": round(score, 6)} for key, score in ranked]


def search_code(query: str, limit: int = 10, mode: str = "hybrid") -> list[dict[str, Any]]:
    import psycopg2

    candidate_pool = limit * 3

    conn = psycopg2.connect(PG_URL)
    try:
        with conn.cursor() as cur:
            dense_results = []
            bm25_results = []

            if mode in ("hybrid", "dense"):
                embedding = _embed_query(query)
                embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
                cur.execute(
                    """
                    SELECT id, filepath, chunk_index, code,
                           1 - (embedding <=> %s::vector) AS score
                    FROM cocoindex.kuadrant_code_embeddings
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (embedding_str, embedding_str, candidate_pool),
                )
                dense_results = [
                    {"id": r[0], "filepath": r[1], "chunk_index": r[2],
                     "code": r[3], "dense_score": round(float(r[4]), 4)}
                    for r in cur.fetchall()
                ]

            if mode in ("hybrid", "bm25"):
                tsquery = " & ".join(
                    t + ":*" for t in query.split() if t.strip()
                )
                if tsquery:
                    cur.execute(
                        """
                        SELECT id, filepath, chunk_index, code,
                               ts_rank_cd(search_vector, to_tsquery('simple', %s)) AS score
                        FROM cocoindex.kuadrant_code_embeddings
                        WHERE search_vector @@ to_tsquery('simple', %s)
                        ORDER BY score DESC
                        LIMIT %s
                        """,
                        (tsquery, tsquery, candidate_pool),
                    )
                    bm25_results = [
                        {"id": r[0], "filepath": r[1], "chunk_index": r[2],
                         "code": r[3], "bm25_score": round(float(r[4]), 4)}
                        for r in cur.fetchall()
                    ]
    finally:
        conn.close()

    if mode == "dense":
        return [
            {**r, "score": r["dense_score"]} for r in dense_results[:limit]
        ]
    if mode == "bm25":
        return [
            {**r, "score": r["bm25_score"]} for r in bm25_results[:limit]
        ]

    fused = _rrf_fuse(dense_results, bm25_results, limit)
    return [{**r, "score": r["rrf_score"]} for r in fused]


def _format_results(query: str, results: list[dict], mode: str = "hybrid") -> str:
    if not results:
        return f"No code results found for: {query}"

    label = {"hybrid": "hybrid (dense+BM25)", "dense": "dense only", "bm25": "BM25 only"}
    lines = [f"Code search [{label.get(mode, mode)}]: {len(results)} results for \"{query}\"\n"]
    for i, r in enumerate(results, 1):
        filepath = r["filepath"]
        score = r["score"]
        sources = []
        if r.get("dense_score") is not None:
            sources.append(f"dense:{r['dense_score']}")
        if r.get("bm25_score") is not None:
            sources.append(f"bm25:{r['bm25_score']}")
        source_info = f" [{', '.join(sources)}]" if sources else ""
        code = r["code"]
        if len(code) > 500:
            code = code[:500] + f"\n... ({len(code)} chars total)"
        lines.append(f"[{i}] {filepath} (score: {score}{source_info})")
        lines.append(code)
        lines.append("")
    return "\n".join(lines)


def pattern_search_code(pattern: str, language: str, limit: int = 10) -> list[dict[str, Any]]:
    """Structural ("by-example") code search via CocoIndex's CodePattern --
    tree-sitter AST matching against the live Kuadrant checkouts. Only
    repos matching `language` are searched (mirrors code_main's per-repo
    language selection)."""
    from cocoindex.ops.code import CodePattern, render_match
    from cocoindex.ops.text import detect_code_language

    cp = CodePattern(pattern, language)
    results: list[dict[str, Any]] = []
    for repo_tag, root, included, excluded in _PATTERN_SEARCH_ROOTS:
        if _REPO_LANGUAGES.get(repo_tag) != language:
            continue
        if len(results) >= limit:
            break
        for path in chunking.find_code_files(root, included, excluded):
            if len(results) >= limit:
                break
            if detect_code_language(filename=path.name) != language:
                continue
            file_match = cp.match_file(str(path))
            if file_match is None:
                continue
            rel_path = path.relative_to(root)
            for match in file_match.matches:
                if len(results) >= limit:
                    break
                view = render_match(file_match.source, match)
                results.append({
                    "repo": repo_tag,
                    "filepath": f"{repo_tag}/{rel_path}",
                    "line": match.chunks[0].start.line,
                    "text": view.text,
                })
    return results


def _format_pattern_results(pattern: str, language: str, results: list[dict]) -> str:
    if not results:
        return f'No structural matches for language={language} pattern: {pattern}'

    lines = [f"Structural pattern search [{language}]: {len(results)} matches for: {pattern}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['filepath']}:{r['line']}")
        lines.append(r["text"])
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Call graph -- CLI/local-use only (not exposed via the cross-mounted
# kuadrant gateway surface, see module docstring). One graph per language:
# callgraph.build_multi_repo_call_graph_with_stats() takes a single
# `language`, so a mixed Go+Rust org resolves each language's repos
# independently rather than merging into one graph -- consistent with the
# existing per-org precedent (praxis.py/dcm.py are each single-language;
# no onboarded org has needed a cross-language merge yet).
# ---------------------------------------------------------------------------

def _roots_for_language(language: str) -> list[tuple[str, pathlib.Path, list[str], list[str]]]:
    return [r for r in _PATTERN_SEARCH_ROOTS if _REPO_LANGUAGES.get(r[0]) == language]


def _build_graph_with_timing(language: str):
    return callgraph.build_multi_repo_call_graph_with_stats(
        _roots_for_language(language), language=language, logger=log,
    )


def call_graph_blast_radius(function: str, depth: int = 2, language: str = "go") -> dict[str, Any]:
    """Who (transitively) calls `function`, up to `depth` hops -- "what
    breaks if I change this," scoped to one language's repos at a time."""
    graph = _build_graph_with_timing(language)
    return callgraph.query_blast_radius(graph, function, depth=depth)


def call_graph_shortest_path(source: str, target: str, language: str = "go") -> dict[str, Any]:
    """Does `source` ever reach `target` through a chain of calls, and how."""
    graph = _build_graph_with_timing(language)
    return callgraph.query_shortest_path(graph, source, target)


def call_graph_get_cluster(function: str, language: str = "go") -> dict[str, Any]:
    """Which Leiden community `function` belongs to, and its other members."""
    graph = _build_graph_with_timing(language)
    return callgraph.query_get_cluster(graph, function)


def _format_blast_radius_result(result: dict) -> str:
    return callgraph.format_blast_radius_result(result)


def _format_shortest_path_result(result: dict) -> str:
    return callgraph.format_shortest_path_result(result)


def _format_cluster_result(result: dict) -> str:
    return callgraph.format_cluster_result(result)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

def _run_mcp_server(host: str = "127.0.0.1", port: int = 8889, transport: str = "stdio") -> None:
    # mcp==2.0.0 (2026-08-22 dependabot bump) renamed FastMCP to MCPServer
    # and moved host/port from the constructor to run(). See
    # docs/findings/2026-08.md's 2026-08-27 entry.
    from mcp.server.mcpserver import MCPServer as FastMCP

    mcp = FastMCP("kuadrant-code")

    @mcp.tool()
    def kuadrant_code_search(query: str, limit: int = 10) -> str:
        """Hybrid code search over the Kuadrant Go/Rust codebases (7 repos:
        kuadrant-operator, limitador, wasm-shim, authorino, dns-operator,
        authorino-operator, limitador-operator -- `architecture` has no
        source code). Prior-art reference material for praxis-proxy.

        Combines dense vector similarity and BM25 keyword matching via
        Reciprocal Rank Fusion. Works equally well for:
        - conceptual queries: "how does limitador rate limit requests?"
        - exact identifiers: "AuthConfig"

        Returns ranked code snippets with file paths and relevance scores.
        Prefer this over Grep when searching by concept rather than exact text.
        """
        results = search_code(query, limit=min(limit, 20))
        return _format_results(query, results)

    @mcp.tool()
    def kuadrant_code_pattern_search(pattern: str, language: str = "go", limit: int = 10) -> str:
        r"""Structural ("by-example") code search over the Kuadrant
        Go/Rust codebases, scoped to one language's repos at a time.

        For "find code shaped like X" -- e.g. every function matching a
        signature -- not "find code about X" (use kuadrant_code_search for
        that). Matches by tree-sitter AST shape, not text/regex.

        Pattern syntax: write an example of the shape you want, using `\`
        + a name for a metavariable (matches one node) or `\(NAME*\)`
        (matches zero or more, e.g. an argument list). Omit a body entirely
        to mean "don't care what's inside".
        """
        results = pattern_search_code(pattern, language, limit=min(limit, 20))
        return _format_pattern_results(pattern, language, results)

    @mcp.tool()
    def kuadrant_call_graph_blast_radius(function: str, depth: int = 2, language: str = "go") -> str:
        """What (transitively) calls `function` across one language's
        Kuadrant repos (go or rust), up to `depth` hops.

        Call resolution is purely name-based (no type info) and per-repo
        only. Rebuilds the call graph fresh on every call (no persisted
        index). See docs/CALL_GRAPH_CLUSTERING.md.
        """
        result = call_graph_blast_radius(function, depth=depth, language=language)
        return _format_blast_radius_result(result)

    @mcp.tool()
    def kuadrant_call_graph_shortest_path(source: str, target: str, language: str = "go") -> str:
        """Does `source` ever reach `target` through a chain of calls
        within one language's Kuadrant repos (go or rust), and how."""
        result = call_graph_shortest_path(source, target, language=language)
        return _format_shortest_path_result(result)

    @mcp.tool()
    def kuadrant_call_graph_get_cluster(function: str, language: str = "go") -> str:
        """Which cluster of related functions (via Leiden community
        detection over the call graph) `function` belongs to, within one
        language's Kuadrant repos."""
        result = call_graph_get_cluster(function, language=language)
        return _format_cluster_result(result)

    if transport == "stdio":
        log.info("Starting kuadrant-code MCP server (stdio)")
        mcp.run(transport="stdio")
    else:
        log.info("Starting kuadrant-code MCP server on %s:%d (sse)", host, port)
        mcp.run(transport="sse", host=host, port=port)


# ---------------------------------------------------------------------------
# CLI query mode
# ---------------------------------------------------------------------------

def _run_cli_query(query: str, limit: int = 10, mode: str = "hybrid") -> None:
    results = search_code(query, limit=limit, mode=mode)
    print(_format_results(query, results, mode=mode))


def _run_cli_pattern_query(pattern: str, language: str, limit: int = 10) -> None:
    results = pattern_search_code(pattern, language, limit=limit)
    print(_format_pattern_results(pattern, language, results))


def _run_cli_blast_radius(function: str, depth: int, language: str) -> None:
    result = call_graph_blast_radius(function, depth=depth, language=language)
    print(_format_blast_radius_result(result))


def _run_cli_shortest_path(source: str, target: str, language: str) -> None:
    result = call_graph_shortest_path(source, target, language=language)
    print(_format_shortest_path_result(result))


def _run_cli_cluster(function: str, language: str) -> None:
    result = call_graph_get_cluster(function, language=language)
    print(_format_cluster_result(result))


def main():
    parser = argparse.ArgumentParser(
        description="Kuadrant code search — MCP server + CLI"
    )
    parser.add_argument("--query", "-q", help="Run a single query and exit")
    parser.add_argument("--pattern", help="Run a single structural pattern query and exit")
    parser.add_argument("--language", default="go", choices=["go", "rust"], help="Language scope (default: go)")
    parser.add_argument("--limit", "-n", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--mode", "-m", default="hybrid", choices=["hybrid", "dense", "bm25"],
                        help="Search mode (default: hybrid)")
    parser.add_argument("--blast-radius", help="Call graph: who (transitively) calls this function")
    parser.add_argument("--depth", type=int, default=2, help="Depth for --blast-radius (default: 2)")
    parser.add_argument("--shortest-path", nargs=2, metavar=("SOURCE", "TARGET"),
                        help="Call graph: shortest call chain SOURCE -> TARGET")
    parser.add_argument("--cluster", help="Call graph: which Leiden cluster this function belongs to")
    parser.add_argument("--port", "-p", type=int, default=8889, help="MCP server port (default: 8889)")
    parser.add_argument("--host", default="127.0.0.1", help="MCP server bind address")
    args = parser.parse_args()

    if args.pattern:
        _run_cli_pattern_query(args.pattern, args.language, limit=args.limit)
    elif args.blast_radius:
        _run_cli_blast_radius(args.blast_radius, depth=args.depth, language=args.language)
    elif args.shortest_path:
        _run_cli_shortest_path(*args.shortest_path, language=args.language)
    elif args.cluster:
        _run_cli_cluster(args.cluster, language=args.language)
    elif args.query:
        _run_cli_query(args.query, limit=args.limit, mode=args.mode)
    else:
        _run_mcp_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
