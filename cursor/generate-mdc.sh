#!/usr/bin/env bash
#
# Generate a project-specific hindsight-memory.mdc from the template.
#
# Usage:
#   ./generate-mdc.sh <config-file> [output-file]
#
# Config file is a simple KEY=VALUE file. Required keys:
#   DOMAIN_TRIGGERS, DOCS_BANK, DOCS_BANK_DESCRIPTION, ISSUES_BANK,
#   CODE_SEARCH_TOOL, CODE_SEARCH_SERVER, EXAMPLE_CONCEPT_QUERY,
#   EXAMPLE_SEMANTIC_QUERY_1, EXAMPLE_SEMANTIC_QUERY_2
#
# Example:
#   ./generate-mdc.sh kubernaut.vars hindsight-memory.mdc

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$SCRIPT_DIR/hindsight-memory.mdc.tmpl"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <config-file> [output-file]"
    echo ""
    echo "Config files in $SCRIPT_DIR/projects/:"
    ls "$SCRIPT_DIR/projects/" 2>/dev/null || echo "  (none yet)"
    exit 1
fi

CONFIG="$1"
OUTPUT="${2:-hindsight-memory.mdc}"

if [ ! -f "$CONFIG" ]; then
    # Try relative to projects/ directory
    if [ -f "$SCRIPT_DIR/projects/$CONFIG" ]; then
        CONFIG="$SCRIPT_DIR/projects/$CONFIG"
    else
        echo "Error: config file not found: $CONFIG"
        exit 1
    fi
fi

if [ ! -f "$TEMPLATE" ]; then
    echo "Error: template not found: $TEMPLATE"
    exit 1
fi

# Source the config
# shellcheck disable=SC1090
source "$CONFIG"

# Verify required variables
REQUIRED_VARS=(
    DOMAIN_TRIGGERS DOCS_BANK DOCS_BANK_DESCRIPTION ISSUES_BANK
    CODE_SEARCH_TOOL CODE_SEARCH_SERVER EXAMPLE_CONCEPT_QUERY
    EXAMPLE_SEMANTIC_QUERY_1 EXAMPLE_SEMANTIC_QUERY_2
)
for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var:-}" ]; then
        echo "Error: missing required variable: $var"
        exit 1
    fi
done

# Perform substitutions
sed \
    -e "s|{{DOMAIN_TRIGGERS}}|$DOMAIN_TRIGGERS|g" \
    -e "s|{{DOCS_BANK}}|$DOCS_BANK|g" \
    -e "s|{{DOCS_BANK_DESCRIPTION}}|$DOCS_BANK_DESCRIPTION|g" \
    -e "s|{{ISSUES_BANK}}|$ISSUES_BANK|g" \
    -e "s|{{CODE_SEARCH_TOOL}}|$CODE_SEARCH_TOOL|g" \
    -e "s|{{CODE_SEARCH_SERVER}}|$CODE_SEARCH_SERVER|g" \
    -e "s|{{EXAMPLE_CONCEPT_QUERY}}|$EXAMPLE_CONCEPT_QUERY|g" \
    -e "s|{{EXAMPLE_SEMANTIC_QUERY_1}}|$EXAMPLE_SEMANTIC_QUERY_1|g" \
    -e "s|{{EXAMPLE_SEMANTIC_QUERY_2}}|$EXAMPLE_SEMANTIC_QUERY_2|g" \
    "$TEMPLATE" > "$OUTPUT"

echo "Generated: $OUTPUT"
