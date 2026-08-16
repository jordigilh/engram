#!/usr/bin/env bash
#
# Generate a family's self-healing git-hook scripts from the templates in
# family/*.sh.tmpl, substituting {{FAMILY_NAME}}/{{DAEMON_LABELS}}/
# {{DEBOUNCE_SECONDS}} from a vars file. Mirrors cursor/generate-mdc.sh's
# pattern for Cursor rules.
#
# Usage:
#   ./generate-hooks.sh <config-file> [output-dir]
#
# Config file is a simple KEY=VALUE file. Required keys:
#   FAMILY_NAME    e.g. "kubernaut-family" -- must match the launchd label
#                  suffix / .cursor/mcp.json template filename prefix you
#                  use elsewhere for this family.
#   DAEMON_LABELS  space-separated full launchd labels to restart, e.g.
#                  "io.vectorize.serena.kubernaut-family
#                   io.vectorize.serena-project-server"
#                  See family/_restart-family-daemons.sh.tmpl's header for
#                  which daemons actually belong here (only ones with real
#                  per-checkout staleness risk -- not DB-backed code search,
#                  not stateless relays).
# Optional keys (default shown):
#   DEBOUNCE_SECONDS=30
#
# Example (see families/kubernaut-family.vars for the real, currently
# deployed kubernaut-family configuration this documents):
#   ./generate-hooks.sh families/kubernaut-family.vars /tmp/generated-hooks

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/family"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <config-file> [output-dir]"
    echo ""
    echo "Config files in $SCRIPT_DIR/families/:"
    ls "$SCRIPT_DIR/families/" 2>/dev/null || echo "  (none yet)"
    exit 1
fi

CONFIG="$1"
OUTPUT_DIR="${2:-$HOME/.hindsight/git-hooks}"

if [ ! -f "$CONFIG" ]; then
    if [ -f "$SCRIPT_DIR/families/$CONFIG" ]; then
        CONFIG="$SCRIPT_DIR/families/$CONFIG"
    else
        echo "Error: config file not found: $CONFIG"
        exit 1
    fi
fi

# shellcheck disable=SC1090
source "$CONFIG"

: "${DEBOUNCE_SECONDS:=30}"

if [ -z "${FAMILY_NAME:-}" ] || [ -z "${DAEMON_LABELS:-}" ]; then
    echo "Error: FAMILY_NAME and DAEMON_LABELS are required in $CONFIG"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

for tmpl in "$TEMPLATE_DIR"/*.sh.tmpl; do
    base=$(basename "$tmpl" .sh.tmpl)
    # e.g. "post-checkout-family-mcp" -> "post-checkout-kubernaut-family-mcp"
    out_name=${base/family/$FAMILY_NAME}
    out_path="$OUTPUT_DIR/${out_name}.sh"
    sed \
        -e "s|{{FAMILY_NAME}}|$FAMILY_NAME|g" \
        -e "s|{{DAEMON_LABELS}}|$DAEMON_LABELS|g" \
        -e "s|{{DEBOUNCE_SECONDS}}|$DEBOUNCE_SECONDS|g" \
        "$tmpl" > "$out_path"
    chmod +x "$out_path"
    echo "Generated: $out_path"
done

echo ""
echo "Next: symlink these into each family repo's .git/hooks/ per"
echo "docs/NEW_PROJECT_SETUP.md step 14, e.g.:"
echo "  ln -sf $OUTPUT_DIR/post-checkout-${FAMILY_NAME}-mcp.sh \"\$d/.git/hooks/post-checkout\""
