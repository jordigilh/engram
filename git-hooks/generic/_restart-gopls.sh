#!/bin/sh
# Shared helper: kill any running `gopls mcp` process whose cwd is the given
# repo toplevel, so Cursor's MCP client respawns a fresh one (via the
# `cd ${workspaceFolder} && exec gopls mcp` command in this repo's mcp.json)
# on the next gopls tool call.
#
# Why this exists: gopls is a long-lived process reused across branch
# switches on the same live clone. It does not reliably detect an
# out-of-band `git checkout`/`git pull` that rewrites files on disk, so its
# in-memory index can silently go stale relative to what's actually checked
# out. Called from post-checkout, post-merge, and reference-transaction.
#
# Usage: _restart-gopls.sh <toplevel-path>
# Safe to call with no matching process running (silent no-op).
# No-op (harmless) for any repo/language that doesn't use gopls at all.

TOPLEVEL="$1"
[ -n "$TOPLEVEL" ] || exit 0

for pid in $(pgrep -f "gopls mcp" 2>/dev/null); do
    cwd=$(lsof -a -p "$pid" -d cwd 2>/dev/null | awk 'NR==2{print $NF}')
    if [ "$cwd" = "$TOPLEVEL" ]; then
        kill "$pid" 2>/dev/null
    fi
done

exit 0
