#!/bin/sh
# Generic (single-repo, non-family) self-healing git hook. Restarts any
# stale `gopls mcp` / `serena start-mcp-server` process for THIS repo on
# checkout (including `git worktree add`) and self-provisions
# post-merge/reference-transaction siblings alongside itself so
# `git pull`/`git merge`/`git reset`/`git rebase` get the same treatment.
#
# Use this variant for a standalone project with its own real (non-symlink)
# .cursor/mcp.json and no shared multi-repo daemon. If this project is one
# of several repos sharing one long-lived HTTP MCP daemon (the way
# kubernaut's 6 repos share one Serena/CocoIndex daemon each), use
# ../family/ instead -- see docs/NEW_PROJECT_SETUP.md step 8a and step 14.
#
# Deliberately does NOT touch .cursor/mcp.json: a standalone project's
# mcp.json is a real, per-repo file, not a symlink into a shared template,
# so there's nothing to (re-)provision here.
#
# Safe to re-run; safe with no matching gopls/serena process running.

set -e

TOPLEVEL=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
HOOKS_SRC_DIR=$(dirname "$0")
[ -L "$0" ] && HOOKS_SRC_DIR=$(dirname "$(readlink "$0")")
GIT_HOOKS_DIR=$(git rev-parse --git-path hooks 2>/dev/null) || GIT_HOOKS_DIR=""

if [ -n "$GIT_HOOKS_DIR" ] && [ -d "$GIT_HOOKS_DIR" ] && [ ! -e "$GIT_HOOKS_DIR/post-merge" ]; then
    ln -sf "$HOOKS_SRC_DIR/post-merge-generic-mcp.sh" "$GIT_HOOKS_DIR/post-merge" 2>/dev/null || true
fi
if [ -n "$GIT_HOOKS_DIR" ] && [ -d "$GIT_HOOKS_DIR" ] && [ ! -e "$GIT_HOOKS_DIR/reference-transaction" ]; then
    ln -sf "$HOOKS_SRC_DIR/reference-transaction-generic-mcp.sh" "$GIT_HOOKS_DIR/reference-transaction" 2>/dev/null || true
fi

if [ -f "$HOOKS_SRC_DIR/_restart-gopls.sh" ]; then
    "$HOOKS_SRC_DIR/_restart-gopls.sh" "$TOPLEVEL" 2>/dev/null || true
fi

if [ -f "$HOOKS_SRC_DIR/_restart-serena.sh" ]; then
    "$HOOKS_SRC_DIR/_restart-serena.sh" "$TOPLEVEL" 2>/dev/null || true
fi

# Optional personal extension point: if you keep a machine-local Cursor rule
# (e.g. a tool-preference override that depends on a personal install and
# must never be committed to this repo), symlink it in here the same way,
# guarded by `[ -f "$SOME_PERSONAL_SRC" ]` so this template stays a true
# no-op for anyone who hasn't set one up. Not included by default -- this
# template only owns MCP-daemon staleness, nothing personal/project-specific.
