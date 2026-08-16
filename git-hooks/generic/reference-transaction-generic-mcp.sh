#!/bin/sh
# Generic (single-repo) sibling of post-checkout-generic-mcp.sh. Restarts
# any stale per-repo `gopls mcp` / `serena start-mcp-server` process after
# *any* operation that moves the currently-checked-out commit -- not just
# `git checkout`/`git merge` (already covered by post-checkout/post-merge),
# but also `git reset` and `git rebase`, neither of which has a dedicated
# git hook of its own. `reference-transaction` fires for every ref-updating
# git command, so it's the one hook that sees all of these uniformly.
#
# Safety: the exit status of this hook is ignored in "committed" state (see
# `git help githooks`), but ALWAYS exits 0 defensively anyway -- a
# non-zero exit during "preparing"/"prepared" states would abort the git
# transaction, which must never happen here regardless of Git's documented
# behavior for "committed".
#
# Self-provisioned by post-checkout-generic-mcp.sh on its next run in any
# repo that doesn't already have this hook installed.

[ "$1" = "committed" ] || exit 0

TOPLEVEL=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
HOOKS_SRC_DIR=$(dirname "$0")
[ -L "$0" ] && HOOKS_SRC_DIR=$(dirname "$(readlink "$0")")

CURRENT_REF=$(git symbolic-ref -q HEAD 2>/dev/null) || CURRENT_REF=""

head_changed=0
while read -r _old _new ref; do
    if [ "$ref" = "HEAD" ]; then
        head_changed=1
    elif [ -n "$CURRENT_REF" ] && [ "$ref" = "$CURRENT_REF" ]; then
        head_changed=1
    fi
done

if [ "$head_changed" = "1" ]; then
    if [ -f "$HOOKS_SRC_DIR/_restart-gopls.sh" ]; then
        "$HOOKS_SRC_DIR/_restart-gopls.sh" "$TOPLEVEL" 2>/dev/null || true
    fi
    if [ -f "$HOOKS_SRC_DIR/_restart-serena.sh" ]; then
        "$HOOKS_SRC_DIR/_restart-serena.sh" "$TOPLEVEL" 2>/dev/null || true
    fi
fi

exit 0
