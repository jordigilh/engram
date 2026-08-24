#!/usr/bin/env python3
r"""Engram code search MCP server.

Provides hybrid code search (dense vectors + BM25) over the
cocoindex.engram_code_embeddings table (this repo's own Python source).
Results are fused using Reciprocal Rank Fusion (RRF) so both semantic
similarity and exact keyword matches contribute to ranking.

Usage:
    python3 engram-cocoindex-search.py                    # Start MCP server (stdio)
    python3 engram-cocoindex-search.py --query "how does contradiction resolution work"
    python3 engram-cocoindex-search.py --query "resolve_contradiction" --mode dense
    python3 engram-cocoindex-search.py --query "resolve_contradiction" --mode bm25
    python3 engram-cocoindex-search.py --pattern 'def \NAME(\(A*\)):' --language python
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
log = logging.getLogger("engram-cocoindex-search")

PG_URL = os.environ.get(
    "COCOINDEX_PG_URL",
    "postgresql://hindsight:hindsight@localhost:5432/hindsight",
)
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
RRF_K = 60  # RRF constant — standard value from the original paper

# Same env var (and default) as engram-cocoindex-flows.py, so pattern search
# walks the exact same checkout the ingestion flow indexes.
ENGRAM_REPO_DIR = pathlib.Path(os.environ.get(
    "ENGRAM_REPO_DIR", os.path.expanduser("~/.hindsight/watch/engram"),
))

_EXCLUDED_PY_PATTERNS = [
    "**/__pycache__/**", "**/.pytest_cache/**", "**/.git/**",
    "**/venv/**", "**/.venv/**", "**/node_modules/**",
]

# (repo_tag, root, included_patterns, excluded_patterns) -- mirrors
# engram-cocoindex-flows.py's localfs.walk_dir(path_matcher=
# PatternFilePathMatcher(...)) call exactly.
_PATTERN_SEARCH_ROOTS = [
    ("engram", ENGRAM_REPO_DIR, ["**/*.py"], _EXCLUDED_PY_PATTERNS),
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
                    FROM cocoindex.engram_code_embeddings
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
                        FROM cocoindex.engram_code_embeddings
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
    tree-sitter AST matching against this repo's own live checkout.

    Unlike search_code() above, there is no structural-pattern index to
    query: CodePattern.match_file() parses source directly, so this walks
    the same file set engram-cocoindex-flows.py already ingests (see
    _PATTERN_SEARCH_ROOTS / chunking.find_code_files()) for every call.
    Complements, not replaces, search_code() (semantic/BM25 "what does X
    do"): this is purely syntactic "find code shaped like X", with no type
    resolution and no cross-file symbol graph (see docs/FINDINGS.md
    2026-08-07).
    """
    from cocoindex.ops.code import CodePattern, render_match
    from cocoindex.ops.text import detect_code_language

    cp = CodePattern(pattern, language)
    results: list[dict[str, Any]] = []
    for repo_tag, root, included, excluded in _PATTERN_SEARCH_ROOTS:
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
    """Format structural pattern matches as readable text for the agent."""
    if not results:
        return f'No structural matches for language={language} pattern: {pattern}'

    lines = [f"Structural pattern search [{language}]: {len(results)} matches for: {pattern}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['filepath']}:{r['line']}")
        lines.append(r["text"])
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Call-graph queries (spike -- see docs/CALL_GRAPH_CLUSTERING.md, issue #43)
# ---------------------------------------------------------------------------
#
# Live-rebuild-per-query, deliberately (scope confirmed for this spike): every
# call below re-walks this repo's own checkout via callgraph.build_call_graph()
# rather than reading a persisted index. Whether a larger repo needs
# persistence is a decision for later, made from the elapsed-time/node/edge
# numbers logged here, not decided up front.

def _build_graph_with_timing():
    import time

    start = time.monotonic()
    graph = callgraph.build_call_graph(
        ENGRAM_REPO_DIR, included=["**/*.py"], excluded=_EXCLUDED_PY_PATTERNS, language="python",
    )
    elapsed = time.monotonic() - start
    log.info(
        "call graph built in %.2fs (%d nodes, %d edges, %d/%d calls unresolved)",
        elapsed, graph.number_of_nodes(), graph.number_of_edges(),
        graph.graph.get("unresolved_calls", 0), graph.graph.get("total_calls", 0),
    )
    return graph


def _resolve_node(graph, identifier: str) -> tuple[str | None, list[str]]:
    """Resolve a user-given identifier to exactly one graph node.

    Accepts either a full qualified name ("search/engram.py::pattern_search_code")
    or a bare function name ("pattern_search_code") -- the latter resolves
    only if unambiguous. Returns (resolved_name, candidates); resolved_name
    is None if zero or more-than-one match was found, in which case
    candidates lists what was found (empty if truly nothing matched)."""
    if identifier in graph.nodes:
        return identifier, [identifier]
    candidates = [n for n in graph.nodes if n.rsplit("::", 1)[-1] == identifier]
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def call_graph_blast_radius(function: str, depth: int = 2) -> dict[str, Any]:
    """Who (transitively) calls `function`, up to `depth` hops -- "what
    breaks if I change this." See module docstring above and
    docs/CALL_GRAPH_CLUSTERING.md for the accuracy ceiling (name-based
    resolution, no type info)."""
    graph = _build_graph_with_timing()
    resolved, candidates = _resolve_node(graph, function)
    if resolved is None:
        return {"error": f"'{function}' not found or ambiguous", "candidates": candidates}

    callers_by_depth: list[list[str]] = []
    seen = {resolved}
    frontier = [resolved]
    for _ in range(depth):
        next_frontier = []
        for node in frontier:
            for pred in graph.predecessors(node):
                if pred not in seen:
                    seen.add(pred)
                    next_frontier.append(pred)
        if not next_frontier:
            break
        callers_by_depth.append(sorted(next_frontier))
        frontier = next_frontier

    return {
        "function": resolved,
        "callers_by_depth": callers_by_depth,
        "unresolved_calls": graph.graph.get("unresolved_calls", 0),
        "total_calls": graph.graph.get("total_calls", 0),
    }


def call_graph_shortest_path(source: str, target: str) -> dict[str, Any]:
    """Does `source` ever reach `target` through a chain of calls, and how."""
    import networkx as nx

    graph = _build_graph_with_timing()
    resolved_source, source_candidates = _resolve_node(graph, source)
    resolved_target, target_candidates = _resolve_node(graph, target)
    if resolved_source is None:
        return {"error": f"'{source}' not found or ambiguous", "candidates": source_candidates}
    if resolved_target is None:
        return {"error": f"'{target}' not found or ambiguous", "candidates": target_candidates}

    try:
        path = nx.shortest_path(graph, resolved_source, resolved_target)
    except nx.NetworkXNoPath:
        return {"source": resolved_source, "target": resolved_target, "path": None}
    return {"source": resolved_source, "target": resolved_target, "path": path}


def call_graph_get_cluster(function: str) -> dict[str, Any]:
    """Which Leiden community `function` belongs to, and its other members.

    Clustering quality depends entirely on the underlying graph's structure:
    a small, centralized codebase may legitimately produce one dominant
    cluster or many singletons -- that reflects the codebase, not a broken
    clustering step (see docs/CALL_GRAPH_CLUSTERING.md)."""
    graph = _build_graph_with_timing()
    resolved, candidates = _resolve_node(graph, function)
    if resolved is None:
        return {"error": f"'{function}' not found or ambiguous", "candidates": candidates}

    clusters = callgraph.compute_clusters(graph)
    cluster_id = clusters.get(resolved)
    members = sorted(n for n, c in clusters.items() if c == cluster_id)
    return {"function": resolved, "cluster_id": cluster_id, "members": members}


def _format_blast_radius_result(result: dict) -> str:
    if "error" in result:
        return _format_lookup_error(result)
    lines = [f"Blast radius for {result['function']}:"]
    if not result["callers_by_depth"]:
        lines.append("  (nothing in this repo calls it, directly or transitively)")
    for depth_i, callers in enumerate(result["callers_by_depth"], 1):
        lines.append(f"  depth {depth_i}: " + ", ".join(callers))
    lines.append(
        f"\n(name-based resolution, no type info -- {result['unresolved_calls']}/{result['total_calls']} "
        "calls in this repo could not be resolved to a known definition; see docs/CALL_GRAPH_CLUSTERING.md)"
    )
    return "\n".join(lines)


def _format_shortest_path_result(result: dict) -> str:
    if "error" in result:
        return _format_lookup_error(result)
    if result["path"] is None:
        return f"No call path found from {result['source']} to {result['target']}."
    return f"{result['source']} -> {result['target']}:\n  " + " -> ".join(result["path"])


def _format_cluster_result(result: dict) -> str:
    if "error" in result:
        return _format_lookup_error(result)
    lines = [f"{result['function']} is in cluster {result['cluster_id']} ({len(result['members'])} members):"]
    lines.extend(f"  {m}" for m in result["members"])
    return "\n".join(lines)


def _format_lookup_error(result: dict) -> str:
    candidates = result.get("candidates") or []
    message = result["error"]
    if candidates:
        return f"{message}, candidates: " + ", ".join(candidates)
    return f"{message} (use a qualified name like 'search/engram.py::pattern_search_code')"


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

def _run_mcp_server(host: str = "127.0.0.1", port: int = 8890, transport: str = "stdio") -> None:
    from mcp.server import FastMCP

    mcp = FastMCP(
        "engram-code",
        host=host,
        port=port,
    )

    @mcp.tool()
    def engram_code_search(query: str, limit: int = 10) -> str:
        """Hybrid code search over the Engram tooling codebase.

        Combines dense vector similarity and BM25 keyword matching via
        Reciprocal Rank Fusion for best results.  Works equally well for:
        - conceptual queries: "how does contradiction resolution work?"
        - exact identifiers: "resolve_contradiction"

        Returns ranked code snippets with file paths and relevance scores.
        Prefer this over Grep when searching by concept rather than exact text.
        """
        results = search_code(query, limit=min(limit, 20))
        return _format_results(query, results)

    @mcp.tool()
    def engram_code_pattern_search(pattern: str, language: str = "python", limit: int = 10) -> str:
        r"""Structural ("by-example") code search over the Engram tooling codebase.

        For "find code shaped like X" -- e.g. every function matching a
        signature -- not "find code about X" (use engram_code_search for
        that). Matches by tree-sitter AST shape, not text/regex.

        Pattern syntax: write an example of the shape you want, using `\`
        + a name for a metavariable (matches one node) or `\(NAME*\)`
        (matches zero or more, e.g. an argument list). Omit a body entirely
        to mean "don't care what's inside" -- e.g. `def \NAME(\(A*\)):`
        matches any Python function/method regardless of body or args.

        This is purely syntactic: it does NOT resolve types and can't find
        references/callers or diagnostics.
        """
        results = pattern_search_code(pattern, language, limit=min(limit, 20))
        return _format_pattern_results(pattern, language, results)

    @mcp.tool()
    def engram_call_graph_blast_radius(function: str, depth: int = 2) -> str:
        """What (transitively) calls `function` in the Engram tooling codebase,
        up to `depth` hops -- "what breaks if I change this."

        `function` may be a bare name ("pattern_search_code") if unambiguous,
        or a qualified name ("search/engram.py::pattern_search_code").

        SPIKE, engram-only (docs/CALL_GRAPH_CLUSTERING.md, issue #43): call
        resolution is purely name-based (no type info), so common method
        names shared across unrelated functions can produce false-positive
        edges, and dynamic dispatch/external calls can't be seen at all.
        Rebuilds the call graph fresh on every call (no persisted index).
        """
        result = call_graph_blast_radius(function, depth=depth)
        return _format_blast_radius_result(result)

    @mcp.tool()
    def engram_call_graph_shortest_path(source: str, target: str) -> str:
        """Does `source` ever reach `target` through a chain of calls in the
        Engram tooling codebase, and how.

        Same name-based-resolution caveat as engram_call_graph_blast_radius
        applies (see its docstring) -- this is a SPIKE, engram-only.
        """
        result = call_graph_shortest_path(source, target)
        return _format_shortest_path_result(result)

    @mcp.tool()
    def engram_call_graph_get_cluster(function: str) -> str:
        """Which cluster of related functions (via Leiden community detection
        over the call graph) `function` belongs to in the Engram tooling
        codebase, and its other members.

        Same name-based-resolution caveat as engram_call_graph_blast_radius
        applies (see its docstring) -- this is a SPIKE, engram-only. A small,
        centralized codebase may legitimately produce one dominant cluster or
        many singletons; that reflects the codebase, not a broken tool.
        """
        result = call_graph_get_cluster(function)
        return _format_cluster_result(result)

    if transport == "stdio":
        log.info("Starting engram-code MCP server (stdio)")
        mcp.run(transport="stdio")
    else:
        log.info("Starting engram-code MCP server on %s:%d (sse)", host, port)
        mcp.run(transport="sse")


# ---------------------------------------------------------------------------
# CLI query mode
# ---------------------------------------------------------------------------

def _run_cli_query(query: str, limit: int = 10, mode: str = "hybrid") -> None:
    results = search_code(query, limit=limit, mode=mode)
    print(_format_results(query, results, mode=mode))


def _run_cli_pattern_query(pattern: str, language: str, limit: int = 10) -> None:
    results = pattern_search_code(pattern, language, limit=limit)
    print(_format_pattern_results(pattern, language, results))


def _run_cli_blast_radius(function: str, depth: int) -> None:
    result = call_graph_blast_radius(function, depth=depth)
    print(_format_blast_radius_result(result))


def _run_cli_shortest_path(source: str, target: str) -> None:
    result = call_graph_shortest_path(source, target)
    print(_format_shortest_path_result(result))


def _run_cli_cluster(function: str) -> None:
    result = call_graph_get_cluster(function)
    print(_format_cluster_result(result))


def main():
    parser = argparse.ArgumentParser(
        description="Engram code search — MCP server + CLI"
    )
    parser.add_argument("--query", "-q", help="Run a single query and exit")
    parser.add_argument("--pattern", help="Run a single structural pattern query and exit")
    parser.add_argument("--language", default="python", help="Language for --pattern (default: python)")
    parser.add_argument("--limit", "-n", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--mode", "-m", default="hybrid", choices=["hybrid", "dense", "bm25"],
                        help="Search mode (default: hybrid)")
    parser.add_argument("--blast-radius", help="Call-graph spike: who (transitively) calls this function")
    parser.add_argument("--depth", type=int, default=2, help="Depth for --blast-radius (default: 2)")
    parser.add_argument("--shortest-path", nargs=2, metavar=("SOURCE", "TARGET"),
                        help="Call-graph spike: shortest call chain SOURCE -> TARGET")
    parser.add_argument("--cluster", help="Call-graph spike: which Leiden cluster this function belongs to")
    parser.add_argument("--port", "-p", type=int, default=8890, help="MCP server port (default: 8890)")
    parser.add_argument("--host", default="127.0.0.1", help="MCP server bind address")
    args = parser.parse_args()

    if args.pattern:
        _run_cli_pattern_query(args.pattern, args.language, limit=args.limit)
    elif args.blast_radius:
        _run_cli_blast_radius(args.blast_radius, depth=args.depth)
    elif args.shortest_path:
        _run_cli_shortest_path(*args.shortest_path)
    elif args.cluster:
        _run_cli_cluster(args.cluster)
    elif args.query:
        _run_cli_query(args.query, limit=args.limit, mode=args.mode)
    else:
        _run_mcp_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
