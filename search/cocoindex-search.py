#!/usr/bin/env python3
r"""CocoIndex code search MCP server.

Provides hybrid code search (dense vectors + BM25) over the
cocoindex.code_embeddings table. Results are fused using Reciprocal Rank
Fusion (RRF) so both semantic similarity and exact keyword matches
contribute to ranking.

Usage:
    python3 cocoindex-search.py                    # Start MCP server (stdio)
    python3 cocoindex-search.py --query "how does the reconciler handle errors"
    python3 cocoindex-search.py --query "ParseConfig" --mode dense   # dense only
    python3 cocoindex-search.py --query "ParseConfig" --mode bm25    # BM25 only
    python3 cocoindex-search.py --pattern 'func \NAME(\(A*\)) error' --language go
"""

import argparse
import logging
import os
import pathlib
import re
import subprocess
import sys
from typing import Any

# This file lives in search/; shared modules (chunking.py etc.) live in the
# src/engram/ package. sys.path[0] for a script invoked via a symlink (as
# launchd does) resolves to the symlink's realpath target directory (search/),
# not the symlink's own directory, so src/ must be added explicitly for
# `engram` to resolve.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from engram import chunking  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("cocoindex-search")

PG_URL = os.environ.get(
    "COCOINDEX_PG_URL",
    "postgresql://hindsight:hindsight@localhost:5432/hindsight",
)
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
RRF_K = 60  # RRF constant — standard value from the original paper

# Same env vars (and defaults) as cocoindex-flows.py, so a launchd plist or
# .env that already configures the ingestion flow's source directories also
# configures pattern search's live file walk with no extra setup.
KUBERNAUT_CODE_DIR = pathlib.Path(os.environ.get(
    "ENGRAM_CODE_DIR", os.path.expanduser("~/.hindsight/watch/kubernaut"),
))
KUBERNAUT_OPERATOR_DIR = pathlib.Path(os.environ.get(
    "ENGRAM_OPERATOR_DIR", os.path.expanduser("~/.hindsight/watch/kubernaut-operator"),
))
KUBERNAUT_CONSOLE_DIR = pathlib.Path(os.environ.get(
    "ENGRAM_CONSOLE_DIR", os.path.expanduser("~/.hindsight/watch/kubernaut-console"),
))

# (repo_tag, root, included_patterns, excluded_patterns) -- mirrors the
# localfs.walk_dir(path_matcher=PatternFilePathMatcher(...)) calls in
# cocoindex-flows.py exactly, so pattern search never drifts out of sync
# with what's actually indexed. kubernaut-console mixes .ts/.tsx (two
# distinct tree-sitter languages); language filtering happens per-file via
# detect_code_language() in pattern_search_code(), not per-root here.
_PATTERN_SEARCH_ROOTS = [
    ("kubernaut", KUBERNAUT_CODE_DIR,
     ["**/*.go"], ["**/vendor/**", "**/*_test.go", "**/zz_generated*"]),
    ("kubernaut-operator", KUBERNAUT_OPERATOR_DIR,
     ["**/*.go"], ["**/vendor/**", "**/*_test.go", "**/zz_generated*"]),
    ("kubernaut-console", KUBERNAUT_CONSOLE_DIR,
     ["**/*.ts", "**/*.tsx"],
     ["**/node_modules/**", "**/dist/**", "**/storybook-static/**", "**/*.d.ts"]),
]

# --- Multi-branch (2026-08-10) ------------------------------------------
#
# code_main (cocoindex-flows.py) additionally indexes release/v1.5 and
# release/v1.6 mirrors of the kubernaut family, tagging rows with
# repo_tag="{repo}@release-{line}" (docs/issues stay main-only, unaffected
# -- see docs/FINDINGS.md 2026-08-03 and its 2026-08-10 refinement). This
# section makes search_code()/pattern_search_code() branch-aware: by
# default they auto-detect which release line (if any) the *caller's* live
# checkout is on via KUBERNAUT_LIVE_CLONE_DIR and scope results to match,
# so `cocoindex_search` from a release/v1.5 workspace doesn't silently
# return main's code. Kept in sync by hand with cocoindex-flows.py's
# KUBERNAUT_RELEASE_LINES (same env var name/default).
KUBERNAUT_RELEASE_LINES = [
    line.strip()
    for line in os.environ.get("KUBERNAUT_RELEASE_LINES", "v1.5,v1.6").split(",")
    if line.strip()
]
# Set by mcp.json (per-workspace ${workspaceFolder} substitution in the
# kubernaut-family templates) to whichever live dev clone this MCP server
# instance was spawned alongside -- NOT one of the read-only mirrors, since
# it's the *caller's actual checkout* we need to detect, not what's mirrored.
KUBERNAUT_LIVE_CLONE_DIR = os.environ.get("KUBERNAUT_LIVE_CLONE_DIR")


def _release_line_dir(repo_name: str, line: str) -> pathlib.Path:
    """Mirror path for one (repo, release line) pair -- must match
    cocoindex-flows.py's `_release_line_dir` (and, transitively,
    watch-mirrors-config.sh's RELEASE_WATCH_MIRRORS mirror_path convention)
    exactly, or pattern search will silently walk nothing."""
    return pathlib.Path(os.path.expanduser(f"~/.hindsight/watch/{repo_name}-release-{line}"))


for _repo_name, _root, _included, _excluded in [
    ("kubernaut", KUBERNAUT_CODE_DIR,
     ["**/*.go"], ["**/vendor/**", "**/*_test.go", "**/zz_generated*"]),
    ("kubernaut-operator", KUBERNAUT_OPERATOR_DIR,
     ["**/*.go"], ["**/vendor/**", "**/*_test.go", "**/zz_generated*"]),
    ("kubernaut-console", KUBERNAUT_CONSOLE_DIR,
     ["**/*.ts", "**/*.tsx"],
     ["**/node_modules/**", "**/dist/**", "**/storybook-static/**", "**/*.d.ts"]),
]:
    for _line in KUBERNAUT_RELEASE_LINES:
        _PATTERN_SEARCH_ROOTS.append((
            f"{_repo_name}@release-{_line}", _release_line_dir(_repo_name, _line),
            _included, _excluded,
        ))
del _repo_name, _root, _included, _excluded, _line


def _detect_current_release_line() -> str | None:
    """Detect which release line (if any) the KUBERNAUT_LIVE_CLONE_DIR
    checkout belongs to, trying two signals in order:

    1. The checked-out branch is literally `release/vX.Y`.
    2. Directory-name convention fallback: this team's actual day-to-day
       workflow is a dedicated clone *per release line* (e.g. `kubernaut`,
       `kubernaut-v1.5`, `kubernaut-v1.6`), with feature/fix branches for
       that line branched off *inside* the matching directory rather than
       worked on directly on the release branch -- so signal 1 almost never
       fires in practice. Confirmed live 2026-08-10: kubernaut-v1.5's
       checked-out `fix/2086-...` branch has merge-base == origin/release/v1.5
       HEAD (0 commits ahead) vs 269 commits ahead of origin/main, i.e. it
       really is v1.5-line work, just not literally *on* that branch.

    Returns None for `main`, a plain (non-suffixed) clone directory, any
    line not in KUBERNAUT_RELEASE_LINES, or when the env var isn't set --
    callers then fall back to the pre-2026-08-10 default (main-only,
    excluding @-tagged rows)."""
    if not KUBERNAUT_LIVE_CLONE_DIR:
        return None

    branch = ""
    try:
        result = subprocess.run(
            ["git", "-C", KUBERNAUT_LIVE_CLONE_DIR, "branch", "--show-current"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass

    match = re.match(r"^release/v(.+)$", branch)
    if match:
        line = f"v{match.group(1)}"
        if line in KUBERNAUT_RELEASE_LINES:
            return line

    dirname = pathlib.Path(KUBERNAUT_LIVE_CLONE_DIR).name
    dir_match = re.search(r"-v(\d+\.\d+)$", dirname)
    if dir_match:
        line = f"v{dir_match.group(1)}"
        if line in KUBERNAUT_RELEASE_LINES:
            return line

    return None


def _resolve_release_line(branch: str | None) -> str | None:
    """Resolve the effective release line to scope results to: an explicit
    `branch` param wins over auto-detection. Returns the release line
    string (e.g. "v1.5") to filter results TO, or None to filter release
    content OUT entirely (covers both "main" and "no line detected" --
    they mean the same thing to callers: today's pre-2026-08-10 behavior).
    `branch="main"` forces the None/exclude behavior explicitly, useful when
    the caller's checkout is on a release branch but they want cross-branch
    (main) results anyway."""
    if branch is None:
        return _detect_current_release_line()
    if branch == "main":
        return None
    return branch if branch in KUBERNAUT_RELEASE_LINES else None


def _branch_where(repo: str | None, release_line: str | None) -> tuple[str, list[str]]:
    """SQL WHERE-clause fragment (leading " AND ...", or "" if nothing to
    filter) + its params, for repo + release-line scoping together. Shared
    by both the dense and BM25 branches of search_code() so they can never
    drift into filtering release-line rows differently from each other."""
    clauses: list[str] = []
    params: list[str] = []
    if release_line is not None:
        clauses.append("filepath LIKE %s")
        params.append(f"{repo}@release-{release_line}/%" if repo else f"%@release-{release_line}/%")
    else:
        if repo:
            clauses.append("filepath LIKE %s")
            params.append(f"{repo}/%")
        # Exclude every release-tagged row from the default/main-branch
        # view -- without this, `cocoindex_search` from a `main` checkout
        # would silently mix release/v1.5-only code into results just
        # because it happens to rank well, which is exactly the
        # cross-branch-mismatch bug this whole feature exists to fix.
        clauses.append("filepath NOT LIKE %s")
        params.append("%@%")
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    return where, params


_model = None


def _get_model():
    """Lazily load the SentenceTransformer model."""
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
    """Fuse two ranked lists using Reciprocal Rank Fusion.

    RRF score = sum(1 / (k + rank_i)) for each list the item appears in.
    """
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


def search_code(
    query: str,
    limit: int = 10,
    mode: str = "hybrid",
    repo: str | None = None,
    branch: str | None = None,
) -> list[dict[str, Any]]:
    """Search the code_embeddings table using hybrid dense + BM25 retrieval.

    filepath is stored as "{repo_tag}/{rel_path}" (see cocoindex-flows.py's
    process_code_file), so passing repo narrows results to one repo (e.g.
    "kubernaut-operator") via a LIKE prefix match.

    branch controls release-line scoping (2026-08-10): omit it to
    auto-detect from KUBERNAUT_LIVE_CLONE_DIR (main/unrecognized -> today's
    behavior, excluding release-tagged rows; a recognized release/vX.Y
    checkout -> scoped to that line's rows only). Pass an explicit release
    line (e.g. "v1.5") to override detection, or "main" to force
    main-only regardless of the caller's actual checkout.
    """
    import psycopg2

    candidate_pool = limit * 3
    release_line = _resolve_release_line(branch)
    branch_where, branch_params = _branch_where(repo, release_line)

    conn = psycopg2.connect(PG_URL)
    try:
        with conn.cursor() as cur:
            dense_results = []
            bm25_results = []

            if mode in ("hybrid", "dense"):
                embedding = _embed_query(query)
                embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
                cur.execute(
                    f"""
                    SELECT id, filepath, chunk_index, code,
                           1 - (embedding <=> %s::vector) AS score
                    FROM cocoindex.code_embeddings
                    WHERE TRUE{branch_where}
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (embedding_str, *branch_params, embedding_str, candidate_pool),
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
                        f"""
                        SELECT id, filepath, chunk_index, code,
                               ts_rank_cd(search_vector, to_tsquery('simple', %s)) AS score
                        FROM cocoindex.code_embeddings
                        WHERE search_vector @@ to_tsquery('simple', %s){branch_where}
                        ORDER BY score DESC
                        LIMIT %s
                        """,
                        (tsquery, tsquery, *branch_params, candidate_pool),
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
    """Format search results as readable text for the agent."""
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


def _select_pattern_roots(repo: str | None, release_line: str | None) -> list[tuple]:
    """Pick which _PATTERN_SEARCH_ROOTS entries apply, given a resolved repo
    scope + release line. release_line=None means main -- the plain,
    untagged repo_tags ("kubernaut", not "kubernaut@release-v1.5")."""
    repo_names = ("kubernaut", "kubernaut-operator", "kubernaut-console")
    if release_line is not None:
        target_tags = {
            f"{r}@release-{release_line}" for r in ((repo,) if repo else repo_names)
        }
    else:
        target_tags = {repo} if repo else set(repo_names)
    return [root for root in _PATTERN_SEARCH_ROOTS if root[0] in target_tags]


def pattern_search_code(
    pattern: str,
    language: str,
    limit: int = 10,
    repo: str | None = None,
    branch: str | None = None,
) -> list[dict[str, Any]]:
    """Structural ("by-example") code search via CocoIndex's CodePattern --
    tree-sitter AST matching against each repo's live checkout.

    Unlike search_code() above, there is no structural-pattern index to
    query: CodePattern.match_file() parses source directly, so this walks
    the same file set cocoindex-flows.py already ingests (see
    _PATTERN_SEARCH_ROOTS / chunking.find_code_files()) for every call.
    Complements, not replaces, search_code() (semantic/BM25 "what does X
    do") and gopls/Serena (type-aware find-references/diagnostics): this is
    purely syntactic "find code shaped like X", with no type resolution and
    no cross-file symbol graph (see docs/FINDINGS.md 2026-08-07).

    branch controls release-line scoping (2026-08-10) exactly like
    search_code(): omit to auto-detect from KUBERNAUT_LIVE_CLONE_DIR, pass
    an explicit release line to override, or "main" to force main-only.
    """
    from cocoindex.ops.code import CodePattern, render_match
    from cocoindex.ops.text import detect_code_language

    release_line = _resolve_release_line(branch)
    roots = _select_pattern_roots(repo, release_line)
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
# MCP server
# ---------------------------------------------------------------------------

def _run_mcp_server(host: str = "127.0.0.1", port: int = 8889, transport: str = "stdio") -> None:
    from mcp.server import FastMCP

    mcp = FastMCP(
        "cocoindex-code",
        host=host,
        port=port,
    )

    @mcp.tool()
    def cocoindex_search(
        query: str, limit: int = 10, repo: str | None = None, branch: str | None = None,
    ) -> str:
        """Hybrid code search over the kubernaut platform codebase.

        Combines dense vector similarity and BM25 keyword matching via
        Reciprocal Rank Fusion for best results.  Works equally well for:
        - conceptual queries: "how does the remediation pipeline work?"
        - exact identifiers: "ParseConfig"

        By default searches the whole platform (kubernaut core + operator +
        console + demo scenarios). Pass repo (e.g. "kubernaut-operator",
        "kubernaut-console") to scope results to just that repo -- use this
        for own-repo work; omit it for upstream/cross-repo triage.

        branch (2026-08-10): the code index additionally covers release/v1.5
        and release/v1.6 mirrors. By default this auto-detects which
        release line your current checkout is on and scopes results to
        match, so results always reflect what you actually have checked
        out -- a `main` (or any feature/fix branch) checkout gets today's
        behavior, a `release/v1.5` checkout gets v1.5's code instead. Pass
        an explicit line (e.g. "v1.5") to override detection (e.g. to check
        a different release line than what you're on), or "main" to force
        main regardless of your checkout. Branches outside {main, v1.5,
        v1.6} aren't indexed here at all -- use Serena for those.

        Returns ranked code snippets with file paths and relevance scores.
        Prefer this over Grep when searching by concept rather than exact text.
        """
        results = search_code(query, limit=min(limit, 20), repo=repo, branch=branch)
        return _format_results(query, results)

    @mcp.tool()
    def cocoindex_pattern_search(
        pattern: str, language: str, limit: int = 10, repo: str | None = None,
        branch: str | None = None,
    ) -> str:
        r"""Structural ("by-example") code search over the kubernaut platform.

        For "find code shaped like X" -- e.g. every function matching a
        signature -- not "find code about X" (use cocoindex_search for
        that). Matches by tree-sitter AST shape, not text/regex.

        Supported languages: "go" (kubernaut, kubernaut-operator), "typescript"
        / "tsx" (kubernaut-console). Pass repo to scope to one of those three.

        branch (2026-08-10): same release-line scoping as cocoindex_search
        -- omit to auto-detect from your current checkout, pass an explicit
        line (e.g. "v1.5") to override, or "main" to force main.

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
        results = pattern_search_code(
            pattern, language, limit=min(limit, 20), repo=repo, branch=branch,
        )
        return _format_pattern_results(pattern, language, results)

    if transport == "stdio":
        log.info("Starting cocoindex-code MCP server (stdio)")
        mcp.run(transport="stdio")
    else:
        log.info("Starting cocoindex-code MCP server on %s:%d (sse)", host, port)
        mcp.run(transport="sse")


# ---------------------------------------------------------------------------
# CLI query mode
# ---------------------------------------------------------------------------

def _run_cli_query(
    query: str, limit: int = 10, mode: str = "hybrid",
    repo: str | None = None, branch: str | None = None,
) -> None:
    results = search_code(query, limit=limit, mode=mode, repo=repo, branch=branch)
    print(_format_results(query, results, mode=mode))


def _run_cli_pattern_query(
    pattern: str, language: str, limit: int = 10,
    repo: str | None = None, branch: str | None = None,
) -> None:
    results = pattern_search_code(pattern, language, limit=limit, repo=repo, branch=branch)
    print(_format_pattern_results(pattern, language, results))


def main():
    parser = argparse.ArgumentParser(
        description="CocoIndex code search — MCP server + CLI"
    )
    parser.add_argument("--query", "-q", help="Run a single query and exit")
    parser.add_argument("--pattern", help="Run a single structural pattern query and exit (requires --language)")
    parser.add_argument("--language", help="Language for --pattern (e.g. go, typescript, tsx)")
    parser.add_argument("--limit", "-n", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--mode", "-m", default="hybrid", choices=["hybrid", "dense", "bm25"],
                        help="Search mode for --query (default: hybrid)")
    parser.add_argument("--repo", default=None,
                        help="Scope results to one repo tag (e.g. kubernaut-operator); default: whole platform")
    parser.add_argument("--branch", default=None,
                        help="Release line to scope to (e.g. v1.5) or 'main'; default: auto-detect from KUBERNAUT_LIVE_CLONE_DIR")
    parser.add_argument("--port", "-p", type=int, default=8889, help="MCP server port (default: 8889)")
    parser.add_argument("--host", default="127.0.0.1", help="MCP server bind address")
    args = parser.parse_args()

    if args.pattern:
        if not args.language:
            parser.error("--pattern requires --language")
        _run_cli_pattern_query(args.pattern, args.language, limit=args.limit, repo=args.repo, branch=args.branch)
    elif args.query:
        _run_cli_query(args.query, limit=args.limit, mode=args.mode, repo=args.repo, branch=args.branch)
    else:
        _run_mcp_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
