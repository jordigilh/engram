#!/usr/bin/env python3
r"""DCM code search MCP server.

Provides hybrid code search (dense vectors + BM25) over the
cocoindex.dcm_code_embeddings table. Results are fused using Reciprocal Rank
Fusion (RRF) so both semantic similarity and exact keyword matches
contribute to ranking.

Usage:
    python3 dcm-cocoindex-search.py                    # Start MCP server (stdio)
    python3 dcm-cocoindex-search.py --query "how does the service provider reconciler work"
    python3 dcm-cocoindex-search.py --query "ParseConfig" --mode dense
    python3 dcm-cocoindex-search.py --query "ParseConfig" --mode bm25
    python3 dcm-cocoindex-search.py --pattern 'func \NAME(\(A*\)) error' --language go
    python3 dcm-cocoindex-search.py --blast-radius Reconcile --depth 2 --repo dcm-control-plane
    python3 dcm-cocoindex-search.py --shortest-path Run Reconcile --repo dcm-control-plane
    python3 dcm-cocoindex-search.py --cluster Reconcile --repo dcm-control-plane
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
log = logging.getLogger("dcm-cocoindex-search")

PG_URL = os.environ.get(
    "COCOINDEX_PG_URL",
    "postgresql://hindsight:hindsight@localhost:5432/hindsight",
)
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
RRF_K = 60

# Same env vars (and defaults) as dcm-cocoindex-flows.py, so pattern search
# walks the exact same checkouts the ingestion flow indexes. Excludes
# DCM_SHARED_WORKFLOWS_DIR -- that repo is shell/YAML, not a tree-sitter
# structural-pattern language.
DCM_CONTROL_PLANE_DIR = pathlib.Path(os.environ.get(
    "DCM_CONTROL_PLANE_DIR", os.path.expanduser("~/go/src/github.com/dcm-project/control-plane"),
))
DCM_CLI_DIR = pathlib.Path(os.environ.get(
    "DCM_CLI_DIR", os.path.expanduser("~/go/src/github.com/dcm-project/cli"),
))
DCM_KUBEVIRT_SP_DIR = pathlib.Path(os.environ.get(
    "DCM_KUBEVIRT_SP_DIR", os.path.expanduser("~/go/src/github.com/dcm-project/kubevirt-service-provider"),
))
DCM_K8S_CONTAINER_SP_DIR = pathlib.Path(os.environ.get(
    "DCM_K8S_CONTAINER_SP_DIR", os.path.expanduser("~/go/src/github.com/dcm-project/k8s-container-service-provider"),
))
DCM_ACM_CLUSTER_SP_DIR = pathlib.Path(os.environ.get(
    "DCM_ACM_CLUSTER_SP_DIR", os.path.expanduser("~/go/src/github.com/dcm-project/acm-cluster-service-provider"),
))
DCM_THREE_TIER_SP_DIR = pathlib.Path(os.environ.get(
    "DCM_THREE_TIER_SP_DIR", os.path.expanduser("~/go/src/github.com/dcm-project/three-tier-app-demo-service-provider"),
))
DCM_OSAC_SP_DIR = pathlib.Path(os.environ.get(
    "DCM_OSAC_SP_DIR", os.path.expanduser("~/go/src/github.com/dcm-project/osac-service-provider"),
))
DCM_UTILITIES_DIR = pathlib.Path(os.environ.get(
    "DCM_UTILITIES_DIR", os.path.expanduser("~/go/src/github.com/dcm-project/utilities"),
))
# osac-project/osac (upstream OSAC backend, read-only, folded into dcm --
# see dcm-cocoindex-flows.py's DCM_OSAC_DIR comment). Points at the
# watch-mirror worktree, not a live dev clone, since this repo is never
# locally edited -- see watch-mirrors-config.sh.
DCM_OSAC_DIR = pathlib.Path(os.environ.get(
    "DCM_OSAC_DIR", os.path.expanduser("~/.hindsight/watch/osac"),
))

_GO_EXCLUDED = ["**/vendor/**", "**/*_test.go", "**/zz_generated*"]

# (repo_tag, root, included_patterns, excluded_patterns) -- mirrors
# dcm-cocoindex-flows.py's go_repos list + localfs.walk_dir(path_matcher=
# PatternFilePathMatcher(...)) call exactly.
_PATTERN_SEARCH_ROOTS = [
    ("dcm-control-plane", DCM_CONTROL_PLANE_DIR, ["**/*.go"], _GO_EXCLUDED),
    ("dcm-cli", DCM_CLI_DIR, ["**/*.go"], _GO_EXCLUDED),
    ("dcm-kubevirt-sp", DCM_KUBEVIRT_SP_DIR, ["**/*.go"], _GO_EXCLUDED),
    ("dcm-k8s-container-sp", DCM_K8S_CONTAINER_SP_DIR, ["**/*.go"], _GO_EXCLUDED),
    ("dcm-acm-cluster-sp", DCM_ACM_CLUSTER_SP_DIR, ["**/*.go"], _GO_EXCLUDED),
    ("dcm-three-tier-sp", DCM_THREE_TIER_SP_DIR, ["**/*.go"], _GO_EXCLUDED),
    ("dcm-osac-sp", DCM_OSAC_SP_DIR, ["**/*.go"], _GO_EXCLUDED),
    ("dcm-utilities", DCM_UTILITIES_DIR, ["**/*.go"], _GO_EXCLUDED),
    ("dcm-osac", DCM_OSAC_DIR, ["**/*.go"], _GO_EXCLUDED),
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
                    FROM cocoindex.dcm_code_embeddings
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
                        FROM cocoindex.dcm_code_embeddings
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


def pattern_search_code(
    pattern: str,
    language: str,
    limit: int = 10,
    repo: str | None = None,
) -> list[dict[str, Any]]:
    """Structural ("by-example") code search via CocoIndex's CodePattern --
    tree-sitter AST matching against each repo's live checkout.

    Unlike search_code() above, there is no structural-pattern index to
    query: CodePattern.match_file() parses source directly, so this walks
    the same file set dcm-cocoindex-flows.py already ingests (see
    _PATTERN_SEARCH_ROOTS / chunking.find_code_files()) for every call.
    Complements, not replaces, search_code() (semantic/BM25 "what does X
    do") and gopls (type-aware find-references/diagnostics): this is purely
    syntactic "find code shaped like X", with no type resolution and no
    cross-file symbol graph (see docs/FINDINGS.md 2026-08-07). Pass repo
    (e.g. "dcm-cli") to scope to one of DCM's 9 Go repos; omit it to search
    all of them.
    """
    from cocoindex.ops.code import CodePattern, render_match
    from cocoindex.ops.text import detect_code_language

    roots = [r for r in _PATTERN_SEARCH_ROOTS if repo is None or r[0] == repo]
    if not roots:
        return []

    cp = CodePattern(pattern, language)
    results: list[dict[str, Any]] = []
    for repo_tag, root, included, excluded in roots:
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
# Call graph (blast radius, shortest path, Leiden clustering)
#
# DCM differs from praxis.py in one respect worth calling out: like
# pattern_search_code above, these accept an optional `repo` to scope the
# build to one of DCM's 9 Go repos (default: all 9, same
# build_multi_repo_call_graph_with_stats path praxis.py uses for its 7 Rust
# repos) -- useful here since 9 repos is a larger multi-repo walk than
# praxis's 7, and most blast-radius/cluster questions are naturally scoped
# to a single repo the caller already knows. Cross-repo call resolution is
# still deliberately NOT attempted regardless of scoping (see
# build_multi_repo_call_graph's docstring in callgraph.py).
# ---------------------------------------------------------------------------

def _build_graph_with_timing(repo: str | None = None):
    roots = [r for r in _PATTERN_SEARCH_ROOTS if repo is None or r[0] == repo]
    return callgraph.build_multi_repo_call_graph_with_stats(
        roots, language="go", logger=log,
    )


def call_graph_blast_radius(function: str, depth: int = 2, repo: str | None = None) -> dict[str, Any]:
    """Who (transitively) calls `function`, up to `depth` hops -- "what
    breaks if I change this." See docs/CALL_GRAPH_CLUSTERING.md for the
    accuracy ceiling (name-based resolution, no type info, and no
    cross-repo resolution -- a call can only resolve within its own repo).
    Pass repo to scope the build to one of DCM's 9 repos."""
    graph = _build_graph_with_timing(repo=repo)
    return callgraph.query_blast_radius(graph, function, depth=depth)


def call_graph_shortest_path(source: str, target: str, repo: str | None = None) -> dict[str, Any]:
    """Does `source` ever reach `target` through a chain of calls, and how."""
    graph = _build_graph_with_timing(repo=repo)
    return callgraph.query_shortest_path(graph, source, target)


def call_graph_get_cluster(function: str, repo: str | None = None) -> dict[str, Any]:
    """Which Leiden community `function` belongs to, and its other members.

    Clustering quality depends entirely on the underlying graph's structure:
    a small, centralized codebase may legitimately produce one dominant
    cluster or many singletons -- that reflects the codebase, not a broken
    clustering step (see docs/CALL_GRAPH_CLUSTERING.md)."""
    graph = _build_graph_with_timing(repo=repo)
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
    from mcp.server import FastMCP

    mcp = FastMCP(
        "dcm-code",
        host=host,
        port=port,
    )

    @mcp.tool()
    def dcm_code_search(query: str, limit: int = 10) -> str:
        """Hybrid code search over the DCM codebase.

        Combines dense vector similarity and BM25 keyword matching via
        Reciprocal Rank Fusion for best results.  Works equally well for:
        - conceptual queries: "how does the service provider reconciler work?"
        - exact identifiers: "ParseConfig"

        Returns ranked code snippets with file paths and relevance scores.
        Prefer this over Grep when searching by concept rather than exact text.
        """
        results = search_code(query, limit=min(limit, 20))
        return _format_results(query, results)

    @mcp.tool()
    def dcm_code_pattern_search(
        pattern: str, language: str = "go", limit: int = 10, repo: str | None = None,
    ) -> str:
        r"""Structural ("by-example") code search over DCM's 9 Go repos.

        For "find code shaped like X" -- e.g. every function matching a
        signature -- not "find code about X" (use dcm_code_search for
        that). Matches by tree-sitter AST shape, not text/regex.

        Pass repo (e.g. "dcm-cli", "dcm-control-plane") to scope to one
        repo; omit it to search all 9.

        Pattern syntax: write an example of the shape you want, using `\`
        + a name for a metavariable (matches one node) or `\(NAME*\)`
        (matches zero or more, e.g. a parameter list). Omit a body entirely
        to mean "don't care what's inside" -- e.g.
        `func \NAME(\(A*\)) (bool, error)` matches any Go function with
        that exact return signature regardless of body or parameter names.

        This is purely syntactic: it does NOT resolve types (won't match
        `(ok bool, err error)` against a search for `(bool, error)`), can't
        find references/callers, and has no diagnostics -- use gopls for
        that. Complementary to, not a replacement for, gopls.
        """
        results = pattern_search_code(pattern, language, limit=min(limit, 20), repo=repo)
        return _format_pattern_results(pattern, language, results)

    @mcp.tool()
    def dcm_call_graph_blast_radius(function: str, depth: int = 2, repo: str | None = None) -> str:
        """What (transitively) calls `function` across DCM's Go repos, up to
        `depth` hops -- "what breaks if I change this."

        `function` may be a bare name ("Reconcile") if unambiguous across
        the repos searched, or a qualified name including the repo tag
        ("dcm-control-plane/internal/reconciler.go::Reconcile"). Pass repo
        (e.g. "dcm-cli") to scope the build to one repo; omit it to search
        all 8 (slower, and more likely to hit bare-name ambiguity across
        repos).

        Call resolution is purely name-based (no type info) and per-repo
        only -- a call in one repo can never resolve to a same-named
        function in a different repo, even if both are searched in one
        call. Common method names shared across unrelated types can
        produce false-positive edges within a repo. See
        docs/CALL_GRAPH_CLUSTERING.md. Rebuilds the call graph fresh on
        every call (no persisted index).
        """
        result = call_graph_blast_radius(function, depth=depth, repo=repo)
        return _format_blast_radius_result(result)

    @mcp.tool()
    def dcm_call_graph_shortest_path(source: str, target: str, repo: str | None = None) -> str:
        """Does `source` ever reach `target` through a chain of calls across
        DCM's Go repos, and how.

        Same name-based, per-repo-only resolution caveat as
        dcm_call_graph_blast_radius applies (see its docstring) -- a path
        can only be found within a single repo, never across two. Pass
        repo to scope the build to one repo (recommended: cross-repo
        blast-radius/path queries can never actually connect across a repo
        boundary anyway, so scoping just saves build time).
        """
        result = call_graph_shortest_path(source, target, repo=repo)
        return _format_shortest_path_result(result)

    @mcp.tool()
    def dcm_call_graph_get_cluster(function: str, repo: str | None = None) -> str:
        """Which cluster of related functions (via Leiden community
        detection over the call graph) `function` belongs to, and its other
        members.

        Same name-based, per-repo-only resolution caveat as
        dcm_call_graph_blast_radius applies (see its docstring). Pass repo
        to scope the build to one repo. A small, centralized module may
        legitimately produce one dominant cluster or many singletons; that
        reflects the codebase, not a broken tool.
        """
        result = call_graph_get_cluster(function, repo=repo)
        return _format_cluster_result(result)

    if transport == "stdio":
        log.info("Starting dcm-code MCP server (stdio)")
        mcp.run(transport="stdio")
    else:
        log.info("Starting dcm-code MCP server on %s:%d (sse)", host, port)
        mcp.run(transport="sse")


# ---------------------------------------------------------------------------
# CLI query mode
# ---------------------------------------------------------------------------

def _run_cli_query(query: str, limit: int = 10, mode: str = "hybrid") -> None:
    results = search_code(query, limit=limit, mode=mode)
    print(_format_results(query, results, mode=mode))


def _run_cli_pattern_query(pattern: str, language: str, limit: int = 10, repo: str | None = None) -> None:
    results = pattern_search_code(pattern, language, limit=limit, repo=repo)
    print(_format_pattern_results(pattern, language, results))


def _run_cli_blast_radius(function: str, depth: int, repo: str | None = None) -> None:
    result = call_graph_blast_radius(function, depth=depth, repo=repo)
    print(_format_blast_radius_result(result))


def _run_cli_shortest_path(source: str, target: str, repo: str | None = None) -> None:
    result = call_graph_shortest_path(source, target, repo=repo)
    print(_format_shortest_path_result(result))


def _run_cli_cluster(function: str, repo: str | None = None) -> None:
    result = call_graph_get_cluster(function, repo=repo)
    print(_format_cluster_result(result))


def main():
    parser = argparse.ArgumentParser(
        description="DCM code search — MCP server + CLI"
    )
    parser.add_argument("--query", "-q", help="Run a single query and exit")
    parser.add_argument("--pattern", help="Run a single structural pattern query and exit")
    parser.add_argument("--language", default="go", help="Language for --pattern (default: go)")
    parser.add_argument("--limit", "-n", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--mode", "-m", default="hybrid", choices=["hybrid", "dense", "bm25"],
                        help="Search mode (default: hybrid)")
    parser.add_argument("--repo", default=None,
                        help="Scope --pattern/call-graph flags to one repo tag (e.g. dcm-cli); default: all 9 repos")
    parser.add_argument("--blast-radius", help="Call graph: who (transitively) calls this function")
    parser.add_argument("--depth", type=int, default=2, help="Depth for --blast-radius (default: 2)")
    parser.add_argument("--shortest-path", nargs=2, metavar=("SOURCE", "TARGET"),
                        help="Call graph: shortest call chain SOURCE -> TARGET")
    parser.add_argument("--cluster", help="Call graph: which Leiden cluster this function belongs to")
    parser.add_argument("--port", "-p", type=int, default=8889, help="MCP server port (default: 8889)")
    parser.add_argument("--host", default="127.0.0.1", help="MCP server bind address")
    args = parser.parse_args()

    if args.pattern:
        _run_cli_pattern_query(args.pattern, args.language, limit=args.limit, repo=args.repo)
    elif args.blast_radius:
        _run_cli_blast_radius(args.blast_radius, depth=args.depth, repo=args.repo)
    elif args.shortest_path:
        _run_cli_shortest_path(*args.shortest_path, repo=args.repo)
    elif args.cluster:
        _run_cli_cluster(args.cluster, repo=args.repo)
    elif args.query:
        _run_cli_query(args.query, limit=args.limit, mode=args.mode)
    else:
        _run_mcp_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
