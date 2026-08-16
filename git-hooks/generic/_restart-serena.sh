#!/bin/sh
# Shared helper: kill any running `serena start-mcp-server --project
# <toplevel>` process for the given repo, so Cursor's MCP client respawns a
# fresh one on the next Serena tool call.
#
# Why this exists: this is the Serena equivalent of _restart-gopls.sh --
# Serena wraps gopls (or another LSP) inside its own long-lived process, so
# it inherits the exact same staleness risk. Matched via the
# `--project <toplevel>` argument (a literal substring match on the full
# command line, not a cwd lookup like _restart-gopls.sh uses) since a
# `uvx`-launched process's cwd is not reliably the repo toplevel.
#
# Usage: _restart-serena.sh <toplevel-path>
# Safe to call with no matching process running (silent no-op).
# Only applicable if this repo's .cursor/mcp.json runs Serena per-repo
# (`--project <toplevel>`) rather than pointing at a shared family daemon --
# see ../family/ for the shared-daemon variant.

TOPLEVEL="$1"
[ -n "$TOPLEVEL" ] || exit 0

for pid in $(pgrep -f "serena start-mcp-server" 2>/dev/null); do
    cmd=$(ps -p "$pid" -o command= 2>/dev/null)
    case "$cmd" in
        *"--project $TOPLEVEL"*|*"--project $TOPLEVEL "*)
            kill "$pid" 2>/dev/null
            ;;
    esac
done

exit 0
