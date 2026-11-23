#!/usr/bin/env bash
#
# Generate a family's self-healing git-hook scripts from the templates in
# family/*.sh.tmpl, substituting {{FAMILY_NAME}}/{{DAEMON_LABELS}}/
# {{DEBOUNCE_SECONDS}} from a vars file.
#
# Usage:
#   ./generate-hooks.sh <config-file> [output-dir]
#
# Config file is a simple KEY=VALUE file. Required keys:
#   FAMILY_NAME    e.g. "kubernaut-family" -- must match the launchd label
#                  suffix you use elsewhere for this family.
#   HOOK_VARIANT   restart (default) | invalidate
#   DAEMON_LABELS  (restart only) space-separated full launchd labels to
#                  restart. See family/_restart-family-daemons.sh.tmpl's header.
#   INVALIDATION_URL, INVALIDATION_TOKEN_FILE, INVALIDATION_SPOOL_DIR
#                  (invalidate only) deployment-owned endpoint, token file, and
#                  durable spool directory.
# Optional keys (default shown):
#   DEBOUNCE_SECONDS=30
#
# Example (see families/kubernaut-family.vars for the real, currently
# deployed kubernaut-family configuration this documents):
#   ./generate-hooks.sh families/kubernaut-family.vars /tmp/generated-hooks

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_TEMPLATE_DIR="$SCRIPT_DIR/family"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <config-file> [output-dir]"
    echo ""
    echo "Config files in $SCRIPT_DIR/families/:"
    ls "$SCRIPT_DIR/families/" 2>/dev/null || echo "  (none yet)"
    exit 1
fi

CONFIG="$1"
OUTPUT_DIR="${2:-$HOME/.engram/git-hooks}"

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
: "${HOOK_VARIANT:=restart}"

if [ -z "${FAMILY_NAME:-}" ]; then
    echo "Error: FAMILY_NAME is required in $CONFIG"
    exit 1
fi

case "$HOOK_VARIANT" in
    restart)
        TEMPLATE_DIR="$DEFAULT_TEMPLATE_DIR"
        OUT_SUFFIX="mcp"
        if [ -z "${DAEMON_LABELS:-}" ]; then
            echo "Error: DAEMON_LABELS is required for HOOK_VARIANT=restart"
            exit 1
        fi
        ;;
    invalidate)
        TEMPLATE_DIR="$SCRIPT_DIR/family-invalidate"
        OUT_SUFFIX="invalidate-mcp"
        for key in INVALIDATION_URL INVALIDATION_TOKEN_FILE INVALIDATION_SPOOL_DIR; do
            if [ -z "${!key:-}" ]; then
                echo "Error: $key is required for HOOK_VARIANT=invalidate"
                exit 1
            fi
        done
        ;;
    *)
        echo "Error: HOOK_VARIANT must be 'restart' or 'invalidate'"
        exit 1
        ;;
esac

mkdir -p "$OUTPUT_DIR"
cp "$TEMPLATE_DIR"/*.py "$OUTPUT_DIR"/ 2>/dev/null || true

for tmpl in "$TEMPLATE_DIR"/*.sh.tmpl; do
    base=$(basename "$tmpl" .sh.tmpl)
    # e.g. "post-checkout-family-mcp" -> "post-checkout-kubernaut-family-mcp"
    # or "...-invalidate-mcp" for the invalidation variant.
    out_name=${base/family-mcp/$FAMILY_NAME-$OUT_SUFFIX}
    out_name=${out_name/family-workspaces/$FAMILY_NAME-workspaces}
    out_name=${out_name/_restart-family-daemons/_restart-$FAMILY_NAME-daemons}
    out_path="$OUTPUT_DIR/${out_name}.sh"
    sed \
        -e "s|{{FAMILY_NAME}}|$FAMILY_NAME|g" \
        -e "s|{{DAEMON_LABELS}}|${DAEMON_LABELS:-}|g" \
        -e "s|{{DEBOUNCE_SECONDS}}|$DEBOUNCE_SECONDS|g" \
        -e "s|{{INVALIDATION_URL}}|${INVALIDATION_URL:-}|g" \
        -e "s|{{INVALIDATION_TOKEN_FILE}}|${INVALIDATION_TOKEN_FILE:-}|g" \
        -e "s|{{INVALIDATION_SPOOL_DIR}}|${INVALIDATION_SPOOL_DIR:-}|g" \
        "$tmpl" > "$out_path"
    chmod +x "$out_path"
    echo "Generated: $out_path"
done

echo ""
echo "Next: symlink these into each family repo's .git/hooks/ per"
echo "docs/NEW_PROJECT_SETUP.md step 14, e.g.:"
echo "  ln -sf $OUTPUT_DIR/post-checkout-${FAMILY_NAME}-${OUT_SUFFIX}.sh \"\$d/.git/hooks/post-checkout\""
