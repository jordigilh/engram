#!/bin/sh
# Generic (single-repo) sibling of post-checkout-generic-mcp.sh. Restarts
# any stale per-repo `gopls mcp` / `serena start-mcp-server` process after
# `git pull`/`git merge` (including plain fast-forwards) rewrites files on
# disk without either noticing.
#
# Self-provisioned by post-checkout-generic-mcp.sh on its next run in any
# repo that doesn't already have this hook installed.
#
# Known gap: stock git has no dedicated post-rebase hook, so a `git rebase`
# that doesn't also trigger a checkout/merge won't restart here either --
# closed by reference-transaction-generic-mcp.sh instead.

set -e

TOPLEVEL=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
HOOKS_SRC_DIR=$(dirname "$0")
[ -L "$0" ] && HOOKS_SRC_DIR=$(dirname "$(readlink "$0")")

if [ -f "$HOOKS_SRC_DIR/_restart-gopls.sh" ]; then
    "$HOOKS_SRC_DIR/_restart-gopls.sh" "$TOPLEVEL" 2>/dev/null || true
fi
if [ -f "$HOOKS_SRC_DIR/_restart-serena.sh" ]; then
    "$HOOKS_SRC_DIR/_restart-serena.sh" "$TOPLEVEL" 2>/dev/null || true
fi

exit 0
